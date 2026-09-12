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
    parser = argparse.ArgumentParser(description="Serve Stock Bot in production.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--threads", type=int,
                        default=int(os.environ.get("WEB_THREADS", "8")))
    args = parser.parse_args(argv)

    # Imported here so that --help works even if something below is missing.
    from waitress import serve

    import web

    cfg = web.config_mod.load(web.app.config.get("CFG_PATH"))
    loop = web.start_alert_loop(cfg)

    public = (os.environ.get("STOCKBOT_PUBLIC_URL") or "").strip()
    print()
    print("  Stock Bot")
    print("    listening on   %s:%d with %d threads"
          % (args.host, args.port, args.threads))
    if public:
        print("    public address %s" % public)
    print("    sign-in        %s"
          % ("Google" if web.auth_required() else "off, single local account"))
    print("    alert checks   %s"
          % ("every %g minutes" % cfg.get("alert_check_minutes", 5)
             if loop else "off"))
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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        sys.exit(0)
