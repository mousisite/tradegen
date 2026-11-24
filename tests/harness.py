"""Give a test its own database and config so it cannot touch real data.

Several suites need a database in a known state, and the obvious way to get one
is to delete the existing file. That destroys a real trade journal and also
makes suites depend on the order they run in. Instead each suite gets a throw
away directory, which is removed when the process ends.
"""
import atexit
import errno
import os
import shutil
import socket
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# web.py and serve.py read .env as they are imported. A suite that has removed
# the real keys must not be given them back: it would call Claude or Stripe.
os.environ["STOCKBOT_NO_DOTENV"] = "1"
# Nor keep one the machine itself has set: a run would call Claude and pay for
# it, and what a suite finds would depend on whose machine it ran on.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("SALES_PAUSED", None)

# Set by isolate(), so a suite can read the paths it was given.
DB_PATH = ""
CFG_PATH = ""
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# Every folder a suite makes with tempfile goes when the process ends. The
# ones suites made for themselves used to stay, some twenty for every full run.
_real_mkdtemp = tempfile.mkdtemp


def _mkdtemp(*args, **kwargs):
    made = _real_mkdtemp(*args, **kwargs)
    atexit.register(shutil.rmtree, made, ignore_errors=True)
    return made


tempfile.mkdtemp = _mkdtemp


def _listed_offline() -> bool:
    """Whether run_all.py lists the suite being run as needing no network."""
    try:
        import run_all
    except ImportError:
        return False
    name = os.path.basename(sys.argv[0] or "")
    return any(s[0] == name and not s[1] for s in run_all.SUITES)


OFFLINE = _listed_offline()


def _no_network(event, args):
    """Refuse any other machine, as an unplugged cable would. A suite that
    needs no network must not depend on one, nor send requests to one."""
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event == "socket.connect" and isinstance(args[1], tuple):
        host = args[1][0]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not host or host in ("localhost", "::1", "0.0.0.0", "::") \
            or host.startswith("127."):
        return
    if event == "socket.getaddrinfo":
        raise socket.gaierror(socket.EAI_NONAME, "no network in an offline suite")
    raise OSError(errno.ENETUNREACH, "no network in an offline suite")


if OFFLINE:
    sys.addaudithook(_no_network)


def isolate(label: str = "suite"):
    """Return (db_path, cfg_path) in a fresh directory, and point web.py there.

    Import web lazily: a suite that never touches the web layer should not pay
    for importing Flask, and should still get an isolated database.
    """
    tmp = tempfile.mkdtemp(prefix="stockbot-test-%s-" % label)

    db_path = os.path.join(tmp, "data", "stockbot.db")
    cfg_path = os.path.join(tmp, "config.json")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Passing a path covers callers that take one. These cover the rest: code
    # under test opens its own connection, and would otherwise reach the real
    # database no matter what the test passed around.
    os.environ["STOCKBOT_DB"] = db_path
    os.environ["STOCKBOT_CONFIG"] = cfg_path

    global DB_PATH, CFG_PATH
    DB_PATH, CFG_PATH = db_path, cfg_path

    if "web" in sys.modules:
        _point_web_at(db_path, cfg_path)
    return db_path, cfg_path


def _point_web_at(db_path: str, cfg_path: str) -> None:
    import web
    web.app.config["DB_PATH"] = db_path
    web.app.config["CFG_PATH"] = cfg_path
    web.app.config["TESTING"] = True
    # Caches are keyed by symbol, not by database, so a suite that reuses the
    # process would otherwise see another suite's cached analysis.
    for attr in ("_CACHE", "_RESEARCH_CACHE"):
        cache = getattr(web, attr, None)
        if isinstance(cache, dict):
            cache.clear()
    if OFFLINE:
        # The front page's own analysis is not asked for where its prices
        # could not arrive.
        web._example_state["busy"] = True


def isolated_web(label: str = "web"):
    """Import web, give it a private database, and hand back a client.

    Returns (test_client, db_path). Use db.connect(db_path) for direct checks so
    the test and the server are certain to be looking at the same file.
    """
    db_path, cfg_path = isolate(label)
    import web
    _point_web_at(db_path, cfg_path)
    return web.app.test_client(), db_path


# Once as this is imported, before any suite imports web: web.py reads or makes
# the cookie key beside the database as it loads, and code under test may load
# the settings file. Both would otherwise be the real ones.
isolate("start")
