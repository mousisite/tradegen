"""A chart screenshot goes to Anthropic only after the person says yes.

App Store guideline 5.1.2(i), and plain fairness: personal data shared with
a third-party AI needs a clear disclosure and an explicit yes first. A
screenshot can show anything a person had on screen, so the Analyse page asks
once, remembers the yes per account, shows it in Settings and takes it back
there. The example account is shared, so its yes is never kept.
"""
import io
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def head(title):
    print("\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72)


for key in ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STOCKBOT_ADMIN_EMAIL"):
    _os.environ.pop(key, None)

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import database as db  # noqa: E402
from bot.vision import ChartRead  # noqa: E402

c, DB = harness.isolated_web("ai-consent")
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"


def png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (900, 500), (250, 249, 245)).save(buf, format="PNG")
    buf.seek(0)
    return buf


def sign_in(user):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with c.session_transaction() as sess:
        sess[web.SESSION_KEY] = token


sent = []


def reader(png):
    sent.append(len(png.getvalue()))
    return ChartRead(symbol="AAPL")


def post(**extra):
    data = {"chart": (png_bytes(), "c.png")}
    data.update(extra)
    return c.post("/analyse/image", data=data, content_type="multipart/form-data")


conn = db.connect(DB)
ann = A.upsert_email_user(conn, "ann@example.com")
conn.close()
sign_in(ann)

real_read = web.read_chart
web.read_chart = reader
_os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
try:
    head("BEFORE THE YES")
    page = c.get("/").data.decode("utf-8", "replace")
    check("the Analyse page asks, with a box to tick",
          'id="ai_ok"' in page and 'type="checkbox"' in page and "Anthropic" in page)
    check("and the box starts unticked", "checked" not in
          page[page.find('id="ai_ok"') - 120:page.find('id="ai_ok"') + 120])
    r = post()
    body = r.data.decode("utf-8", "replace")
    check("a picture sent without the yes is refused, in plain words",
          r.status_code == 400 and "Tick the box" in body, r.status_code)
    check("and nothing went to Anthropic", not sent, sent)
    conn = db.connect(DB)
    check("and no yes was recorded", A.ai_consent(conn, ann.id) == 0)
    conn.close()

    head("THE YES, ONCE")
    r = post(ai_ok="1")
    check("with the box ticked the picture is read", r.status_code == 302 and len(sent) == 1,
          (r.status_code, sent))
    conn = db.connect(DB)
    check("and the yes is kept for the account", A.ai_consent(conn, ann.id) > 0)
    conn.close()
    r = post()
    check("so the next picture needs no box", r.status_code == 302 and len(sent) == 2,
          (r.status_code, sent))
    page = c.get("/").data.decode("utf-8", "replace")
    check("and the page no longer asks", 'type="checkbox"' not in
          page[page.find('id="shotform"'):page.find("</form>", page.find('id="shotform"'))])

    head("TAKING IT BACK")
    page = c.get("/settings").data.decode("utf-8", "replace")
    check("Settings shows the yes and a way to take it back",
          'id="screenshots"' in page and "Stop sending screenshots" in page)
    r = c.post("/settings/ai-consent")
    check("taking it back returns to Settings", r.status_code == 302, r.status_code)
    conn = db.connect(DB)
    check("and removes the yes", A.ai_consent(conn, ann.id) == 0)
    conn.close()
    r = post()
    check("so the next picture is asked about again", r.status_code == 400 and len(sent) == 2,
          (r.status_code, sent))

    head("DELETING THE ACCOUNT")
    post(ai_ok="1")
    conn = db.connect(DB)
    A.delete_user(conn, ann.id)
    left = conn.execute("SELECT COUNT(*) FROM ai_consents WHERE user_id=?",
                        (ann.id,)).fetchone()[0]
    conn.close()
    check("removes the yes with everything else", left == 0, left)

    head("THE PRIVACY PAGE")
    page = c.get("/privacy").data.decode("utf-8", "replace")
    check("says nothing is sent until the box is ticked",
          'id="anthropic"' in page and "Nothing is sent until you tick the box" in page)
finally:
    web.read_chart = real_read
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
if fails:
    print("FAILURES:", fails)
_sys.exit(1 if fails else 0)
