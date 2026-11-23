"""Run the app for real, rather than with Flask's development server.

Flask's built-in server prints a warning telling you not to use it in
production, and the warning is correct: it is single-threaded by default,
handles slow clients badly, and has no request limits. Waitress is a pure
Python WSGI server with none of those problems and no compiled dependencies,
which matters because the whole point of the bundled runtime is that nothing
has to be built on the target machine.

    python serve.py                 port 8000, or $PORT
    python serve.py --port 9000
    python serve.py --threads 8

Everything else is configured through the environment. See DEPLOY.md.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading

# The bundled runtime is an embeddable build, whose ._pth deliberately keeps the
# script's own directory off sys.path. Every entry point has to put it back or
# none of the local modules can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env before anything reads a credential out of the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


# Copies kept of the database as each earlier release left it.
KEEP_RELEASE_COPIES = 3

# And for how long, the newest included. Each holds every account as it was,
# a deleted one too, and the privacy page says a backup holding a deleted
# account is gone in about a week. A release that damaged data shows it within
# days; after that the timed backups are the ones to go back to.
RELEASE_COPY_DAYS = 7


def _code_fingerprint() -> str:
    """A stand-in for the release name when the host gives none: a hash of
    the code that decides the database's shape, which changes whenever a
    release could change it. Empty if the files cannot be read."""
    import glob
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    try:
        for path in sorted([os.path.join(here, "web.py")]
                           + glob.glob(os.path.join(here, "bot", "*.py"))):
            with open(path, "rb") as fh:
                digest.update(os.path.basename(path).encode("utf-8") + b"/" + fh.read())
    except OSError:
        return ""
    return "code-" + digest.hexdigest()[:16]


def _prune_release_copies(db_path) -> None:
    """Delete the copies taken before releases that are past their week.

    Run at every start and after every timed backup, not only when a new
    release takes a copy: otherwise the last few stayed for as long as the
    next releases took, months in a quiet spell. Never raises.
    """
    try:
        import backup as backup_mod
        folder = os.path.dirname(os.path.abspath(db_path))
        backup_mod.prune(os.path.join(folder, "backups", "before-release"),
                         KEEP_RELEASE_COPIES, max_age=RELEASE_COPY_DAYS * 86400.0)
    except Exception as exc:
        print("  could not prune the copies taken before releases: %s: %s"
              % (type(exc).__name__, exc))
    sys.stdout.flush()


def _copy_before_release(db_path) -> None:
    """Back the database up before a new release's migrations touch it.

    The release is the commit the host says it is running (Render sets
    RENDER_GIT_COMMIT), remembered in a file beside the database, so a restart
    of the same release takes no copy. A failed copy is said loudly but does
    not stop the start: the migrations only ever add, and the timed backups
    carry on either way. A copy that does not verify is deleted, as a timed
    backup's is, and the release is not noted, so the next start tries again.
    """
    release = (os.environ.get("RENDER_GIT_COMMIT") or "").strip() or _code_fingerprint()
    if not db_path or not os.path.exists(db_path):
        return
    _prune_release_copies(db_path)
    if not release:
        return
    folder = os.path.dirname(os.path.abspath(db_path))
    marker = os.path.join(folder, "release.txt")
    try:
        with open(marker, encoding="utf-8") as fh:
            if fh.read().strip() == release:
                return
    except OSError:
        pass
    try:
        import backup as backup_mod
        target = os.path.join(folder, "backups", "before-release")
        path = backup_mod.take(target, source=db_path)
        if not backup_mod.verify(path):
            # Kept, it would be trusted on the day a release is undone.
            try:
                os.remove(path)
            except OSError:
                pass
            raise RuntimeError("the copy at %s did not verify, so it was deleted" % path)
        backup_mod.prune(target, KEEP_RELEASE_COPIES)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(release)
        print("  copied the database before release %s" % release[:12])
    except Exception as exc:
        print("  COPY BEFORE RELEASE FAILED: %s: %s" % (type(exc).__name__, exc))
    sys.stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve Orenth in production.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--threads", type=int,
                        default=int(os.environ.get("WEB_THREADS", "8")))
    args = parser.parse_args(argv)

    # Errors the app handles, and failures in the background jobs, are written
    # to the log, and the host keeps whatever the process prints. Without this
    # they went nowhere, and the first sign of a broken job was a user.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # One line per Claude request is noise; its failures still come through.
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    # A backstop for any network call made without its own timeout. Without
    # one, a source that stops answering holds a request thread for ever, and
    # there are only a handful of them.
    socket.setdefaulttimeout(30)

    # Imported here so that --help works even if something below is missing.
    from waitress import serve

    import web
    from bot import database as db_mod
    from bot import monitoring, offsite as offsite_mod, scheduler as scheduler_mod

    # Before anything else, so a failure during start-up is reported too.
    monitoring.start(release=os.environ.get("RENDER_GIT_COMMIT", ""))

    # The database every connection opens. DB_PATH is set only by web.main(),
    # which this file never calls, so here it is STOCKBOT_DB or the default,
    # as database.connect() works it out. Passing DB_PATH alone handed the copy
    # below None on the host, and it was quietly never taken.
    db_path = web.app.config.get("DB_PATH") or db_mod.default_path()

    # A copy of the database as the previous release left it, taken before this
    # release changes anything in it. Once per release, not per restart.
    _copy_before_release(db_path)

    # Once, on this one thread, before anything else can open the database:
    # new columns are added here rather than raced for by the first requests.
    db_mod.migrate(db_path)

    cfg = web.config_mod.load(web.app.config.get("CFG_PATH"))
    loop = web.start_alert_loop(cfg)
    web.start_background_jobs()

    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()
    print()
    print("  Orenth")
    print("    listening on   %s:%d with %d threads"
          % (args.host, args.port, args.threads))
    if public:
        print("    public address %s" % public)
        if not public.lower().startswith("https://"):
            print("    PUBLIC ADDRESS IS NOT HTTPS: STOCKBOT_PUBLIC_URL must start with "
                  "https://, or sign-in cookies go unprotected and Google sign-in fails")
    # The ways in that are actually set up. A public deployment asks people to
    # sign in even when none is, and saying "Google" then sent the operator
    # looking for a fault in Google sign-in rather than for the missing keys.
    ways = [name for name, ready in (("Google", web.auth_mod.configured()),
                                     ("Apple", web.apple_mod.web_ready()),
                                     ("Apple in the app only",
                                      web.apple_mod.native_ready()
                                      and not web.apple_mod.web_ready()),
                                     ("email code", web.codes_mod.email_ready()),
                                     ("text code", web.codes_mod.phone_ready()))
            if ready]
    print("    sign-in        %s"
          % ("off, single local account" if not web.auth_required() else
             ", ".join(ways) if ways else
             "required, but no way to sign in is set up"))
    # Only when some of Apple's settings are there: then the missing ones.
    if web.apple_mod.why_not():
        print("    apple          %s" % web.apple_mod.why_not())
    # App Review's own way in, and why its code would be refused, so a
    # rejection for "the demo account does not work" names its cause here.
    print("    review sign-in %s" % web.review_status())
    # Pro sold in the iPhone app, through Apple.
    print("    app purchases  %s" % web.apple_store_mod.describe())
    print("    alert checks   %s"
          % ("every %g minutes" % cfg.get("alert_check_minutes", 5)
             if loop else "off"))
    print("    error reports  %s" % monitoring.status()["detail"])
    # Asking also makes the browsers' key the first time, here at start-up.
    off = web.push_mod.why_not()
    print("    notifications  browsers %s; iPhone app %s"
          % ("on" if not off["web"] else "off: " + off["web"],
             "on" if not off["apns"] else "off: " + off["apns"]))

    def _backup_failed(exc):
        """A silent backup failure is the worst kind: loud in the log as well.

        The scheduler has already logged it with its traceback, which is also
        what reaches error reporting; this is the line to search the log for.
        """
        print("  BACKUP FAILED: %s: %s" % (type(exc).__name__, exc))
        sys.stdout.flush()

    # A backup nobody runs is not a backup. This runs one on a timer inside the
    # server, keeps the most recent few, and verifies each before keeping it.
    backups = None
    every_hours = float(os.environ.get("STOCKBOT_BACKUP_HOURS", "12") or 0)
    if every_hours > 0:
        import backup as backup_mod

        target = (os.environ.get("STOCKBOT_BACKUP_DIR") or "").strip()
        if not target:
            target = os.path.join(os.path.dirname(db_mod.default_path()), "backups")

        def take_one():
            from bot import database as db_mod
            # First, so a copy taken before a release goes when its week is
            # up even when this backup fails.
            _prune_release_copies(db_path)
            if not os.path.exists(db_mod.default_path()):
                # Nothing has been written yet. Not a failure, and not worth
                # an alert; the next run will find a database.
                print("  backup skipped: no database written yet")
                sys.stdout.flush()
                return {"checked": 0, "fired": []}
            path = backup_mod.take(target, with_csv=True)
            if not backup_mod.verify(path):
                # A backup that does not verify is worse than none, because it
                # is trusted. Remove it and say so loudly.
                try:
                    os.remove(path)
                except OSError:
                    pass
                monitoring.note("A backup failed to verify and was discarded",
                                path=path)
                raise RuntimeError("backup at %s did not verify" % path)
            backup_mod.prune(target, int(os.environ.get("STOCKBOT_BACKUP_KEEP", "8")))
            # Then off this disk, when that is set up. In a thread of its own,
            # so a slow or failing copy never holds up or fails this backup;
            # how it went is logged and shown on /usage.
            offsite_mod.copy_in_background(path)
            # Off a terminal, print is block-buffered, and a backup you cannot
            # see happening is nearly as bad as one that fails quietly.
            sys.stdout.flush()
            return {"checked": 1, "fired": []}

        every = every_hours * 3600.0
        backups = scheduler_mod.Scheduler(
            take_one, interval_seconds=every,
            name="stockbot-backups",
            on_error=_backup_failed)
        backups.start()
        # The scheduler's own first run is a whole interval away, and a deploy
        # restarts that wait, so the first one is brought forward when due.
        first = backup_mod.first_run_in(target, every)
        when = ""
        if first < every - 60:
            soon = threading.Timer(first, backups.check_now)
            soon.daemon = True
            soon.start()
            when = ", the next in %s" % ("%d minutes" % round(first / 60.0)
                                        if first < 5400 else
                                        "%.1f hours" % (first / 3600.0))
        print("    backups        every %g hours into %s%s"
              % (every_hours, target, when))
    else:
        print("    backups        off")
    print("    off-site       %s" % offsite_mod.describe(backups_on=every_hours > 0))
    print()
    sys.stdout.flush()

    try:
        serve(web.app, host=args.host, port=args.port, threads=args.threads,
              # A trading page can take a few seconds to build; the default
              # channel timeout would cut long analyses off.
              channel_timeout=180, ident="stockbot")
    finally:
        if loop is not None:
            loop.stop()
        if backups is not None:
            backups.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        sys.exit(0)
