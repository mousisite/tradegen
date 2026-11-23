"""Take a backup that is actually safe to restore.

The obvious way to back up a SQLite file is to copy it. That is wrong here. The
database runs in WAL mode, so at any moment some committed data lives in the
-wal file rather than the main one, and a plain copy taken while the server is
writing produces a file that may open fine and be missing the most recent
writes, or may not open at all. It is the kind of backup you discover is broken
on the day you need it.

SQLite's own backup API copies a consistent snapshot of a live database while
it is being written to, which is what this uses.

    python backup.py                     writes where the server keeps its own
                                         (STOCKBOT_BACKUP_DIR, or backups beside
                                         STOCKBOT_DB), else into backups/
    python backup.py --out D:\\safe       somewhere else
    python backup.py --keep 30           prune to the newest 30
    python backup.py --csv               also write the plain-text exports
    python backup.py --verify <file>     check a backup opens and is intact
    python backup.py --restore <file>    put a backup in place of the database
                                         (STOCKBOT_DB), then restart the app

Off-site copies, once their settings are in place (see DEPLOY.md). The server
copies each automatic backup by itself; these are for doing it by hand:

    python backup.py --offsite <file>    check a backup, copy it to the bucket
    python backup.py --offsite-list      list the copies kept there
    python backup.py --fetch <name>      download one and check it, to restore
"""
from __future__ import annotations

import argparse
import logging
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
_PART = re.compile(r"^stockbot-(?:[a-z_]+-)?(%s)\.(?:db|db-wal|db-shm|db-journal|csv)$"
                   % _STAMP)
# A copy still being written, or left behind by a server stopped part-way.
_UNFINISHED = re.compile(r"^stockbot-%s\.db\.part(?:-journal|-wal|-shm)?$" % _STAMP)
UNFINISHED_FOR = 3600.0

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


def folder() -> str:
    """Where the server keeps its timed backups."""
    from bot import database as db_mod

    return ((os.environ.get("STOCKBOT_BACKUP_DIR") or "").strip()
            or os.path.join(os.path.dirname(db_mod.default_path()), "backups"))


def _remove_unfinished(part: str) -> None:
    for path in (part, part + "-journal", part + "-wal", part + "-shm"):
        try:
            os.remove(path)
        except OSError:
            pass


def take(out_dir: str, with_csv: bool = False, source: Optional[str] = None) -> str:
    """Copy a consistent snapshot of the live database, or of source.

    Written under a temporary name and given its own only once whole: a copy
    cut off part-way would otherwise sit there as the newest backup, empty.
    """
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
    while os.path.exists(target_path) or os.path.exists(target_path + ".part"):
        target_path = os.path.join(out_dir, "stockbot-%s-%d.db" % (stamp, suffix))
        suffix += 1

    part = target_path + ".part"
    try:
        source = sqlite3.connect(source_path, timeout=30.0)
        try:
            target = sqlite3.connect(part)
            try:
                # The backup API, not a file copy: consistent even mid-write.
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        os.replace(part, target_path)
    except BaseException:
        _remove_unfinished(part)
        raise

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
                path = os.path.join(out_dir, "stockbot-%s-%s.csv" % (name, taken))
                try:
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        count = export_mod.write_csv(conn, name, fh)
                except Exception as exc:
                    # The copy is the backup. Failed over one of these, it
                    # would be neither checked, copied off the server nor
                    # pruned, at this run and every one after.
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    print("  %-9s could not be written: %s: %s"
                          % (name, type(exc).__name__, exc))
                    logging.getLogger("backup").error(
                        "The %s export beside a backup could not be written", name,
                        exc_info=True)
                    continue
                print("  %-9s %s  (%d rows)" % (name, path, count))
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
        if _UNFINISHED.match(name):
            # Old enough that nothing can still be writing it.
            try:
                if now - os.path.getmtime(os.path.join(out_dir, name)) < UNFINISHED_FOR:
                    continue
            except OSError:
                continue
        elif part is None or part.group(1) in kept:
            continue
        try:
            os.remove(os.path.join(out_dir, name))
            print("  removed old backup %s" % name)
        except OSError as exc:
            print("  could not remove %s: %s" % (name, exc))


def how_many(out_dir: str) -> int:
    return len(_copies(out_dir))


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


# How long a restore waits for another program's write, such as the running
# server's, to finish before it gives up and changes nothing. The server's own
# writes take a fraction of a second.
RESTORE_WAIT = 10.0


class _Busy(Exception):
    """Another program held the database's write lock for too long."""


def _write_lock_free(path: str) -> bool:
    """Whether the database's write lock can be had within RESTORE_WAIT."""
    conn = sqlite3.connect(path, timeout=RESTORE_WAIT)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return True
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc) and "busy" not in str(exc):
            raise
        return False
    finally:
        conn.close()


def _said_busy() -> None:
    print("  Something else has held the database's write lock for over %g seconds,"
          % RESTORE_WAIT)
    print("  so nothing was changed. Run this again in a minute, and stop the app "
          "first if it keeps happening.")


def restore(path: str) -> bool:
    """Put a backup in place of the live database, through SQLite itself.

    Copying the file over the database is not a restore. A server that was
    stopped or killed leaves the -wal file beside the database with its last
    writes still in it, and SQLite lays them over the copy at the next open:
    the result opens cleanly and is the backup with newer pages over it.
    SQLite's backup API writes every page of the backup into the database as
    one change, which supersedes whatever the -wal holds, and any other
    connection, the running server's included, sees all of it or none of it.

    The backup is checked first, and the database as it is now is backed up
    beside the timed backups, so a restore from the wrong file can be undone
    the same way. Nothing is changed when the backup does not verify, when it
    is the database itself, or when another program holds the write lock for
    longer than RESTORE_WAIT. The app should be restarted straight after.
    """
    from bot import database as db_mod

    live = db_mod.default_path()
    print("  Checking the backup")
    if not verify(path):
        print("  It is not an intact backup, so nothing was changed.")
        return False
    if os.path.exists(live) and os.path.samefile(path, live):
        print("  That is the live database itself, so nothing was changed.")
        return False
    print()
    undo = ""
    if os.path.exists(live):
        try:
            if not _write_lock_free(live):
                _said_busy()
                return False
            print("  Backing up the database as it is now, so this can be undone")
            undo = take(folder())
        except (OSError, sqlite3.OperationalError) as exc:
            # No room for the copy, or the file could not be opened: going on
            # would leave nothing to undo with.
            print("  %s" % exc)
            print("  Nothing was changed.")
            return False
        except sqlite3.DatabaseError as exc:
            # Not a database, or a damaged one: the usual reason to restore,
            # and there is nothing in it to keep.
            print("  It cannot be read (%s), so no copy of it is kept." % exc)
        print()

    print("  Restoring %s" % path)
    print("  into      %s" % live)

    def waited(status, _remaining, _total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise _Busy()

    try:
        os.makedirs(os.path.dirname(live), exist_ok=True)
        source = sqlite3.connect(path)
        try:
            target = sqlite3.connect(live, timeout=RESTORE_WAIT)
            try:
                source.backup(target, progress=waited)
            finally:
                target.close()
        finally:
            source.close()
    except _Busy:
        _said_busy()
        return False
    except (OSError, sqlite3.DatabaseError) as exc:
        # A failed backup step is rolled back by SQLite.
        print("  That did not work: %s. Nothing was changed." % exc)
        print("  If the database file itself is broken, stop the app, move %s and"
              % os.path.basename(live))
        print("  any -wal and -shm files beside it out of the way, and run this again.")
        return False
    print()
    print("  Checking the restored database")
    if not verify(live):
        print("  It did not verify. Restore another backup%s."
              % (", or undo with %s" % undo if undo else ""))
        return False
    print()
    print("  Restored. Restart the app now, so it brings the restored database up")
    print("  to date and forgets what it read before.")
    if undo:
        print("  To undo, restore %s the same way." % undo)
    return True


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
    # Not by copying it over the database: the -wal file beside that would lay
    # newer writes over the copy.
    print("  To restore from it:  python backup.py --restore %s" % path)
    print("  then restart the app.")
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


def _keep_by_default() -> int:
    """How many to keep when --keep does not say: 14, or the server's own
    number where it keeps more. Backing up by hand into the server's folder
    must never delete a timed backup the server means to keep."""
    try:
        keep = float((os.environ.get("STOCKBOT_BACKUP_KEEP") or "").strip())
    except ValueError:
        return 14
    if not abs(keep) <= 1e9:
        return 14
    return max(14, int(keep)) if int(keep) > 0 else 0


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    # Nobody else on the machine may read what is written: it holds everyone's
    # address and journal, as the cookie key's own file does its secret.
    os.umask(0o077)
    beside_code = os.path.join(here, "backups")
    set_on_server = any((os.environ.get(k) or "").strip()
                        for k in ("STOCKBOT_BACKUP_DIR", "STOCKBOT_DB"))
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="The off-site commands need the four BACKUP_S3_ settings, which "
               "DEPLOY.md explains. Once they are set, the server copies each "
               "automatic backup by itself.")
    parser.add_argument("--out", default="",
                        help="where to write backups, and where --fetch saves one")
    parser.add_argument("--keep", type=int, default=_keep_by_default(),
                        help="how many to keep, 0 for all")
    parser.add_argument("--csv", action="store_true",
                        help="also write plain-text exports")
    parser.add_argument("--verify", metavar="FILE",
                        help="check an existing backup instead of taking one")
    parser.add_argument("--restore", metavar="FILE",
                        help="check a backup, back up the database as it is now, "
                             "then write the backup into it through SQLite; "
                             "restart the app afterwards")
    parser.add_argument("--offsite", metavar="FILE",
                        help="check a backup, then copy it to the off-site bucket "
                             "and check the copy that arrived")
    parser.add_argument("--offsite-list", action="store_true",
                        help="list the copies kept in the off-site bucket, newest first")
    parser.add_argument("--fetch", metavar="NAME",
                        help="download one off-site copy into the --out folder and "
                             "check it, to restore from")
    args = parser.parse_args(argv)
    if not args.out:
        # On a server, where its own backups go: the folder beside the code is
        # inside the container there, and gone at the next deploy. A fetched
        # copy stays out of there, or it would count as the newest backup and
        # the next would wait for it to come due.
        args.out = folder() if set_on_server and not args.fetch else beside_code

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

    if args.restore:
        print()
        ok = restore(args.restore)
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
