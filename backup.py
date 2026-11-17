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

Off-site copies, once their settings are in place (see DEPLOY.md). The server
copies each automatic backup by itself; these are for doing it by hand:

    python backup.py --offsite <file>    check a backup, copy it to the bucket
    python backup.py --offsite-list      list the copies kept there
    python backup.py --fetch <name>      download one and check it, to restore
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# One backup is a database copy and, optionally, its CSV exports, all carrying
# the same stamp: stockbot-<stamp>.db and stockbot-<dataset>-<stamp>.csv.
_STAMP = r"\d{4}-\d{2}-\d{2}-\d{6}(?:-\d+)?"
_COPY = re.compile(r"^stockbot-(%s)\.db$" % _STAMP)
_PART = re.compile(r"^stockbot-(?:[a-z_]+-)?(%s)\.(?:db|db-wal|db-shm|csv)$" % _STAMP)

# Free space a backup must leave behind. Backups live on the same disk as the
# live database, and a backup that fills it stops every write the site makes.
HEADROOM = 50 * 1024 * 1024


def _size(path: str) -> str:
    return _bytes(os.path.getsize(path))


def _bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "bytes" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d bytes" % n


def _free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def take(out_dir: str, with_csv: bool = False, source: Optional[str] = None) -> str:
    """Copy a consistent snapshot of the live database, or of source."""
    from bot import database as db_mod

    source_path = source or db_mod.default_path()
    if not os.path.exists(source_path):
        # A normal exception, not SystemExit. This is called from a scheduler
        # as well as from a command line, and a library that kills the process
        # is a library that cannot be scheduled.
        raise FileNotFoundError("There is no database at %s yet." % source_path)

    os.makedirs(out_dir, exist_ok=True)
    # Refused rather than attempted: a copy that fills the disk breaks the live
    # database it was meant to protect.
    size = sum(os.path.getsize(p) for p in (source_path, source_path + "-wal")
               if os.path.exists(p))
    if _free_bytes(out_dir) < size + HEADROOM:
        raise OSError("There is not enough free disk space for another backup, "
                      "so none was taken. Delete old backups, keep fewer, or "
                      "make the disk larger.")
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
        # Named with the copy's own stamp, suffix included, so prune() can
        # delete them together with it.
        taken = _COPY.match(os.path.basename(target_path)).group(1)
        # A plain connection, because the copy is only read here; connect()
        # would also bring its schema up to date, which writes to the backup.
        conn = sqlite3.connect(target_path)
        conn.row_factory = sqlite3.Row
        try:
            for name in export_mod.DATASETS:
                built = export_mod.build(conn, name, "csv")
                path = os.path.join(out_dir, "stockbot-%s-%s.csv" % (name, taken))
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


def _copies(out_dir: str) -> list:
    """The database copies in a folder, newest first."""
    if not os.path.isdir(out_dir):
        return []
    return sorted((os.path.join(out_dir, f) for f in os.listdir(out_dir)
                   if _COPY.match(f)), key=os.path.getmtime, reverse=True)


def prune(out_dir: str, keep: int, max_age: Optional[float] = None) -> None:
    """Delete all but the newest few, so this can run on a timer.

    A backup's CSV exports go with its database copy. They hold the same
    people's data, so keeping them would keep a deleted account's trades and
    notes for ever, and they would slowly fill the disk the database is on.
    With max_age, a copy more than that many seconds old goes too, however
    few are left.
    """
    if keep <= 0 or not os.path.isdir(out_dir):
        return
    now = time.time()
    kept = {_COPY.match(os.path.basename(p)).group(1)
            for p in _copies(out_dir)[:keep]
            if max_age is None or now - os.path.getmtime(p) <= max_age}
    for name in sorted(os.listdir(out_dir)):
        part = _PART.match(name)
        if part is None or part.group(1) in kept:
            continue
        try:
            os.remove(os.path.join(out_dir, name))
            print("  removed old backup %s" % name)
        except OSError as exc:
            print("  could not remove %s: %s" % (name, exc))


def newest_age(out_dir: str) -> Optional[float]:
    """Seconds since the newest backup in a folder was taken, or None."""
    found = _copies(out_dir)
    return max(0.0, time.time() - os.path.getmtime(found[0])) if found else None


def first_run_in(out_dir: str, every_seconds: float,
                 soon: float = 300.0) -> float:
    """How long after a start the first scheduled backup should run.

    The timer waits a whole interval after the server starts, and every deploy
    restarts it, so on a day of deploys no backup ran at all. When one is due,
    because there is none or the newest is older than the interval, it runs a
    few minutes after start, clear of everything else starting up. Otherwise it
    runs when the newest one comes due.
    """
    age = newest_age(out_dir)
    if age is None or age >= every_seconds:
        return soon
    return max(soon, every_seconds - age)


def _offsite_list(offsite_mod, settings) -> bool:
    copies = offsite_mod.list_copies(settings)
    print("  Off-site copies in %s at %s, under %s"
          % (settings.bucket, settings.host, settings.prefix or "the top level"))
    print()
    if not copies:
        print("  None yet.")
        return True
    for item in copies:
        when = item["modified"][:16].replace("T", " ")
        print("  %-36s %10s   %s" % (item["name"], _bytes(item["size"]),
                                     (when + " UTC") if when else ""))
    print()
    print("  %d in all, %s. Kept there: the newest %d, the newest of each of the "
          "last %d days and the oldest of each of the last %d months."
          % (len(copies), _bytes(sum(i["size"] for i in copies)),
             offsite_mod.KEEP_NEWEST, offsite_mod.KEEP_DAYS, offsite_mod.KEEP_MONTHS))
    return True


def _offsite_fetch(offsite_mod, settings, name: str, out_dir: str) -> bool:
    print("  Fetching %s from %s at %s" % (name, settings.bucket, settings.host))
    path, got = offsite_mod.fetch_copy(name, out_dir, settings)
    print("  saved     %s  (%s)" % (path, _size(path)))
    print("  %s" % ("it matches the backup that was uploaded" if got["checked"] else
                    "no fingerprint was stored with it, so only its size was checked"))
    print()
    print("  Verifying it")
    if not verify(path):
        print("  It downloaded whole but is not an intact backup. Do not restore from it.")
        return False
    print()
    print("  To restore from it, put it in place of the database while the app is stopped.")
    return True


def _offsite_copy(offsite_mod, settings, path: str) -> bool:
    print("  Checking the backup before copying it")
    if not verify(path):
        print("  It did not verify, so it was not copied.")
        return False
    print()
    print("  Copying it to %s at %s" % (settings.bucket, settings.host))
    result = offsite_mod.copy_backup(path, settings=settings)
    if not result["ok"]:
        print("  %s" % result["detail"])
        return False
    print("  stored and checked: %s in %.1f s" % (_size(path), result["seconds"]))
    print("  %s" % (result.get("note") or offsite_mod.tally(result)))
    if not offsite_mod.managed(os.path.basename(path)):
        print("  Its name is not one the automatic history uses, so the app never "
              "deletes it.")
    return True


def _offsite(args) -> bool:
    """The three off-site commands. Prints what happened; True when it worked."""
    from bot import offsite as offsite_mod

    settings, problem = offsite_mod.load()
    if settings is None:
        print("  Off-site copies are not set up%s" % (": " + problem if problem else "."))
        print("  DEPLOY.md, under \"Copies of the backups somewhere else\", has the steps.")
        return False
    try:
        if args.offsite_list:
            return _offsite_list(offsite_mod, settings)
        if args.fetch:
            return _offsite_fetch(offsite_mod, settings, args.fetch, args.out)
        return _offsite_copy(offsite_mod, settings, args.offsite)
    except (offsite_mod.OffsiteError, OSError) as exc:
        print("  %s" % exc)
        return False


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="The off-site commands need the four BACKUP_S3_ settings, which "
               "DEPLOY.md explains. Once they are set, the server copies each "
               "automatic backup by itself.")
    parser.add_argument("--out", default=os.path.join(here, "backups"),
                        help="where to write backups, and where --fetch saves one")
    parser.add_argument("--keep", type=int, default=14,
                        help="how many to keep, 0 for all")
    parser.add_argument("--csv", action="store_true",
                        help="also write plain-text exports")
    parser.add_argument("--verify", metavar="FILE",
                        help="check an existing backup instead of taking one")
    parser.add_argument("--offsite", metavar="FILE",
                        help="check a backup, then copy it to the off-site bucket "
                             "and check the copy that arrived")
    parser.add_argument("--offsite-list", action="store_true",
                        help="list the copies kept in the off-site bucket, newest first")
    parser.add_argument("--fetch", metavar="NAME",
                        help="download one off-site copy into the --out folder and "
                             "check it, to restore from")
    args = parser.parse_args(argv)

    if args.offsite or args.offsite_list or args.fetch:
        print()
        ok = _offsite(args)
        print()
        return 0 if ok else 1

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
    except OSError as exc:
        print("  %s" % exc)
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
    # The app reads its settings from .env as well as the environment, and so
    # must this, or a local install's off-site commands find no settings. Only
    # here, not in main(), so the tests that call main() never pick up a real
    # bucket from a .env file.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass
    sys.exit(main())
