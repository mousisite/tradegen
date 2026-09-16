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
import os
import sys

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve TradeGen in production.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--threads", type=int,
                        default=int(os.environ.get("WEB_THREADS", "8")))
    args = parser.parse_args(argv)

    # Imported here so that --help works even if something below is missing.
    from waitress import serve

    import web
    from bot import monitoring, scheduler as scheduler_mod

    # Before anything else, so a failure during start-up is reported too.
    monitoring.start(release=os.environ.get("RENDER_GIT_COMMIT", ""))

    cfg = web.config_mod.load(web.app.config.get("CFG_PATH"))
    loop = web.start_alert_loop(cfg)

    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()
    print()
    print("  TradeGen")
    print("    listening on   %s:%d with %d threads"
          % (args.host, args.port, args.threads))
    if public:
        print("    public address %s" % public)
    print("    sign-in        %s"
          % ("Google" if web.auth_required() else "off, single local account"))
    print("    alert checks   %s"
          % ("every %g minutes" % cfg.get("alert_check_minutes", 5)
             if loop else "off"))
    print("    error reports  %s" % monitoring.status()["detail"])

    def _backup_failed(exc):
        """A silent backup failure is the worst kind: loud in the log as well."""
        print("  BACKUP FAILED: %s: %s" % (type(exc).__name__, exc))
        sys.stdout.flush()
        monitoring.note("Backup failed: %s" % exc)

    # A backup nobody runs is not a backup. This runs one on a timer inside the
    # server, keeps the most recent few, and verifies each before keeping it.
    backups = None
    every_hours = float(os.environ.get("STOCKBOT_BACKUP_HOURS", "12") or 0)
    if every_hours > 0:
        import backup as backup_mod

        target = (os.environ.get("STOCKBOT_BACKUP_DIR") or "").strip()
        if not target:
            from bot import database as db_mod
            target = os.path.join(os.path.dirname(db_mod.default_path()), "backups")

        def take_one():
            from bot import database as db_mod
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
            # Off a terminal, print is block-buffered, and a backup you cannot
            # see happening is nearly as bad as one that fails quietly.
            sys.stdout.flush()
            return {"checked": 1, "fired": []}

        backups = scheduler_mod.Scheduler(
            take_one, interval_seconds=every_hours * 3600.0,
            name="stockbot-backups",
            on_error=_backup_failed)
        backups.start()
        print("    backups        every %g hours into %s" % (every_hours, target))
    else:
        print("    backups        off")
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
