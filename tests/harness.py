"""Give a test its own database and config so it cannot touch real data.

Several suites need a database in a known state, and the obvious way to get one
is to delete the existing file. That destroys a real trade journal and also
makes suites depend on the order they run in. Instead each suite gets a throw
away directory, which is removed when the process ends.
"""
import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Set by isolate(), so a suite can read the paths it was given.
DB_PATH = ""
CFG_PATH = ""
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def isolate(label: str = "suite"):
    """Return (db_path, cfg_path) in a fresh directory, and point web.py there.

    Import web lazily: a suite that never touches the web layer should not pay
    for importing Flask, and should still get an isolated database.
    """
    tmp = tempfile.mkdtemp(prefix="stockbot-test-%s-" % label)
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)

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


def isolated_web(label: str = "web"):
    """Import web, give it a private database, and hand back a client.

    Returns (test_client, db_path). Use db.connect(db_path) for direct checks so
    the test and the server are certain to be looking at the same file.
    """
    import web
    db_path, cfg_path = isolate(label)
    _point_web_at(db_path, cfg_path)
    return web.app.test_client(), db_path
