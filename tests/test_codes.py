"""Signing in with a code sent by email or text, with no real messages sent."""
import os as _os
import sys as _sys
import time

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

from bot import accounts as A
from bot import codes
from bot import database as db

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


SECRET = b"test-secret"
DB, _CFG = harness.isolate("codes")

print("=" * 72)
print("WHAT PEOPLE TYPE")
print("=" * 72)

check("an email is cleaned", codes.clean_email("  Ann@Example.COM ") == "ann@example.com")
for bad in ("", "ann", "ann@", "@example.com", "ann@example"):
    try:
        codes.clean_email(bad)
        check("%r is refused" % bad, False)
    except codes.CodeError:
        check("%r is refused" % bad, True)

check("a number is reduced to E.164",
      codes.clean_phone("+1 (555) 123-4567") == "+15551234567")
check("a 00 prefix becomes a plus", codes.clean_phone("0044 7700 900123") == "+447700900123")
try:
    codes.clean_phone("555 123 4567")
    check("a number with no country code is refused", False)
except codes.CodeError:
    check("a number with no country code is refused", True)

_os.environ["PHONE_ALLOWED_PREFIXES"] = "+1"
try:
    codes.clean_phone("+447700900123")
    check("a country outside the allowed list is refused", False)
except codes.CodeError:
    check("a country outside the allowed list is refused", True)
finally:
    del _os.environ["PHONE_ALLOWED_PREFIXES"]

print()
print("=" * 72)
print("EMAIL CODES")
print("=" * 72)

conn = db.connect(DB)
sent = {}


def fake_send(to, code):
    sent[to] = code


codes.send_email_code(conn, "ann@example.com", "1.1.1.1", SECRET, fake_send)
code = sent["ann@example.com"]
check("a six-digit code is sent", len(code) == 6 and code.isdigit(), code)
stored = conn.execute("SELECT code_hash FROM login_codes WHERE target='ann@example.com'").fetchone()[0]
check("the code itself is never stored", code not in stored)

wrong = "%06d" % ((int(code) + 1) % 10 ** 6)
try:
    codes.check_email_code(conn, "ann@example.com", wrong, SECRET)
    check("a wrong code is refused", False)
except codes.CodeError as exc:
    check("a wrong code is refused", True)
    check("and says how many tries are left", "tries left" in str(exc), exc)

codes.check_email_code(conn, "ann@example.com", code[:3] + " " + code[3:], SECRET)
check("the right code is accepted, even with a space in it", True)
try:
    codes.check_email_code(conn, "ann@example.com", code, SECRET)
    check("a code works only once", False)
except codes.CodeError:
    check("a code works only once", True)

codes.send_email_code(conn, "bob@example.com", "2.2.2.2", SECRET, fake_send)
for _ in range(codes.MAX_ATTEMPTS):
    try:
        codes.check_email_code(conn, "bob@example.com", "000000"
                               if sent["bob@example.com"] != "000000" else "111111", SECRET)
    except codes.CodeError:
        pass
try:
    codes.check_email_code(conn, "bob@example.com", sent["bob@example.com"], SECRET)
    check("after five wrong tries even the right code is refused", False)
except codes.CodeError:
    check("after five wrong tries even the right code is refused", True)

codes.send_email_code(conn, "cat@example.com", "3.3.3.3", SECRET, fake_send)
conn.execute("UPDATE login_codes SET expires_ts=? WHERE target='cat@example.com'",
             (int(time.time()) - 1,))
conn.commit()
try:
    codes.check_email_code(conn, "cat@example.com", sent["cat@example.com"], SECRET)
    check("an expired code is refused", False)
except codes.CodeError:
    check("an expired code is refused", True)

# Limits: an inbox cannot be flooded, and one connection cannot farm codes.
for _ in range(2):
    codes.send_email_code(conn, "dan@example.com", "4.4.4.%d" % _, SECRET, fake_send)
codes.send_email_code(conn, "dan@example.com", "4.4.4.9", SECRET, fake_send)
try:
    codes.send_email_code(conn, "dan@example.com", "4.4.4.8", SECRET, fake_send)
    check("one address cannot be sent endless codes", False)
except codes.CodeError:
    check("one address cannot be sent endless codes", True)

for n in range(10):
    try:
        codes.send_email_code(conn, "farm%d@example.com" % n, "5.5.5.5", SECRET, fake_send)
    except codes.CodeError:
        break
try:
    codes.send_email_code(conn, "farm99@example.com", "5.5.5.5", SECRET, fake_send)
    check("one connection cannot ask for endless codes", False)
except codes.CodeError:
    check("one connection cannot ask for endless codes", True)

print()
print("=" * 72)
print("PHONE CODES, THROUGH A STAND-IN FOR TWILIO")
print("=" * 72)

calls = []


def fake_twilio(path, data):
    calls.append((path, dict(data)))
    if path == "VerificationCheck":
        return {"status": "approved" if data["Code"] == "424242" else "pending"}
    return {"status": "pending"}


codes.send_phone_code(conn, "+15551234567", "6.6.6.6", fake_twilio)
check("Twilio is asked to send a text",
      calls[-1] == ("Verifications", {"To": "+15551234567", "Channel": "sms"}))
try:
    codes.check_phone_code(conn, "+15551234567", "000000", fake_twilio)
    check("a wrong texted code is refused", False)
except codes.CodeError:
    check("a wrong texted code is refused", True)
codes.check_phone_code(conn, "+15551234567", "424242", fake_twilio)
check("the right texted code is accepted", True)

saved_cap = codes.PHONE_PER_HOUR
codes.PHONE_PER_HOUR = conn.execute(
    "SELECT COUNT(*) FROM login_codes WHERE channel='phone'").fetchone()[0]
try:
    codes.send_phone_code(conn, "+15550000001", "7.7.7.7", fake_twilio)
    check("texts stop at the hourly cap, whatever the number", False)
except codes.CodeError:
    check("texts stop at the hourly cap, whatever the number", True)
finally:
    codes.PHONE_PER_HOUR = saved_cap

print()
print("=" * 72)
print("ACCOUNTS")
print("=" * 72)

google = A.upsert_google_user(conn, {"email": "eve@example.com", "sub": "g-eve",
                                     "name": "Eve", "email_verified": True})
by_email = A.upsert_email_user(conn, "eve@example.com")
check("an emailed code reaches the same account as Google for that address",
      by_email.id == google.id)
first = A.upsert_phone_user(conn, "+15551234567")
again = A.upsert_phone_user(conn, "+15551234567")
check("a phone number always reaches the same account", first.id == again.id)
check("which is shown by its number, not a placeholder address",
      first.contact == "+15551234567" and first.display == "+15551234567")
check("whose placeholder address can never be delivered to",
      first.email.endswith("@users.invalid"))
try:
    A.upsert_email_user(conn, A.LOCAL_EMAIL)
    check("the local account cannot be signed in to by email", False)
except ValueError:
    check("the local account cannot be signed in to by email", True)
conn.close()

print()
print("=" * 72)
print("THE PAGES")
print("=" * 72)

import web  # noqa: E402

for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SMTP_HOST", "SMTP_USER",
            "SMTP_PASSWORD", "MAIL_FROM", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
            "TWILIO_VERIFY_SID"):
    _os.environ.pop(key, None)
c, WDB = harness.isolated_web("codes-web")
web.app.config["AUTH_MODE"] = None
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"
try:
    page = c.get("/signin").data.decode("utf-8", "replace")
    check("with nothing set up, the email button is not offered",
          "Continue with email" not in page)
    check("and its page does not exist", c.get("/signin/email").status_code == 404)

    _os.environ.update({"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u",
                        "SMTP_PASSWORD": "p", "MAIL_FROM": "Orenth <hi@example.com>",
                        "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t",
                        "TWILIO_VERIFY_SID": "VA1"})
    page = c.get("/signin").data.decode("utf-8", "replace")
    check("once configured, email is offered", "Continue with email" in page)
    check("and so is phone", "Continue with phone" in page)

    outbox = {}
    real_send, real_twilio = codes._smtp_send, codes._twilio
    codes._smtp_send = lambda to, code: outbox.__setitem__(to, code)
    codes._twilio = fake_twilio
    try:
        r = c.post("/signin/email", data={"to": "Fay@Example.com", "next": "/trades"})
        check("asking for a code moves on to the code page",
              r.status_code == 302 and "/signin/email/code" in r.headers["Location"],
              r.status_code)
        page = c.get("/signin/email/code").data.decode("utf-8", "replace")
        check("which says where it was sent", "fay@example.com" in page)
        r = c.post("/signin/email/code", data={"code": "999999"
                                               if outbox["fay@example.com"] != "999999" else "888888"})
        check("a wrong code keeps them on the page with a reason",
              r.status_code == 200 and "not right" in r.data.decode("utf-8", "replace"))
        r = c.post("/signin/email/code", data={"code": outbox["fay@example.com"]})
        check("the right code signs them in and goes where they were headed",
              r.status_code == 302 and r.headers["Location"].endswith("/trades"),
              r.headers.get("Location"))
        check("and they really are signed in", c.get("/account").status_code == 200)
        c.post("/signout")

        r = c.post("/signin/phone", data={"to": "+1 555 765 4321"})
        check("a phone number moves on to the code page",
              r.status_code == 302 and "/signin/phone/code" in r.headers["Location"])
        r = c.post("/signin/phone/code", data={"code": "424242"})
        check("the texted code signs them in", r.status_code == 302)
        page = c.get("/account").data.decode("utf-8", "replace")
        check("and their account shows their number", "+15557654321" in page)
        c.post("/signout")

        bad = c.post("/signin/email", data={"to": "not-an-email"})
        check("a bad address is explained, not crashed on",
              bad.status_code == 200
              and "does not look like an email" in bad.data.decode("utf-8", "replace"))
        check("going to the code page with nothing pending starts over",
              c.get("/signin/phone/code").status_code == 302)
    finally:
        codes._smtp_send, codes._twilio = real_send, real_twilio
finally:
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SMTP_HOST", "SMTP_USER",
                "SMTP_PASSWORD", "MAIL_FROM", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SID"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("CODES OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
