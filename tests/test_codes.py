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
for bad in ("", "ann", "ann@", "@example.com", "ann@example",
            # Spellings a mail server delivers to ann@example.com, or to
            # somebody else, that would each have been filed as a new person.
            "ann(1)@example.com", "x:ann@example.com", "y<ann@example.com",
            "\"ann@example.com", "ann\\@example.com", "ann,bob@example.com",
            "ann;bob@example.com", "[ann@example.com", "ann>@example.com",
            "=?utf-8?q?ann?=@example.com", "ann\x01@example.com",
            "ann\u200b@example.com", "ann@\uff45xample.com",
            # Endings that can never be an inbox, a phone account's included.
            "phone15551234567@users.invalid", "example@orenth.invalid",
            "ann@mail.test", "ann@mail.example", "ann@my.localhost"):
    try:
        codes.clean_email(bad)
        check("%a is refused" % bad, False)
    except codes.CodeError:
        check("%a is refused" % bad, True)

for good in ("ann.lee+stocks@example.com", "o'neil@example.co.uk", "a_b-c@x.io",
             "jos\u00e9@example.com", "ann@xn--e1afmkfd.com"):
    check("%a is still an address" % good, codes.clean_email(good) == good)


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self, **k):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append(to_addrs)


_real_smtp = codes.smtplib.SMTP
codes.smtplib.SMTP = FakeSMTP
_os.environ.update({"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u",
                    "SMTP_PASSWORD": "p", "MAIL_FROM": "Orenth <hi@example.com>"})
try:
    codes.send_mail("x:ann@example.com", "s", "b")
finally:
    codes.smtplib.SMTP = _real_smtp
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"):
        _os.environ.pop(key, None)
check("an email goes to exactly the address given, never one read out of it",
      FakeSMTP.sent == [["x:ann@example.com"]], FakeSMTP.sent)

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

# The second of two codes lets the person in, and the first goes with it.
codes.send_email_code(conn, "two@example.com", "2.2.2.9", SECRET, fake_send)
older = sent["two@example.com"]
codes.send_email_code(conn, "two@example.com", "2.2.2.9", SECRET, fake_send)
codes.check_email_code(conn, "two@example.com", sent["two@example.com"], SECRET)
check("a right code is not counted as a wrong guess",
      conn.execute("SELECT SUM(attempts) FROM login_codes WHERE "
                   "target='two@example.com'").fetchone()[0] == 0)
try:
    codes.check_email_code(conn, "two@example.com", older, SECRET)
    check("an older code stops working once a newer one is used", False)
except codes.CodeError as exc:
    check("an older code stops working once a newer one is used",
          "expired" in str(exc), exc)

# Guesses sent at the same moment: each reads the count before any writes it.
# The stand-in holds every guess at that point until all of them are there.
import threading  # noqa: E402

codes.send_email_code(conn, "rush@example.com", "2.2.2.8", SECRET, fake_send)
TOGETHER = 8
gate = threading.Barrier(TOGETHER, timeout=20)
judged, real_hash, judged_lock = [], codes._hash, threading.Lock()
real_check_guesses = codes._check_guesses


def counting_hash(*args):
    with judged_lock:
        judged.append(1)
    return real_hash(*args)


arrived = threading.local()


def waiting_check(*args):
    real_check_guesses(*args)
    if not getattr(arrived, "yes", False):
        arrived.yes = True
        gate.wait()


def guess():
    k = db.connect(DB)
    try:
        codes.check_email_code(k, "rush@example.com", "000000"
                               if sent["rush@example.com"] != "000000" else "111111", SECRET)
    except codes.CodeError:
        pass
    finally:
        k.close()


codes._hash, codes._check_guesses = counting_hash, waiting_check
try:
    threads = [threading.Thread(target=guess) for _ in range(TOGETHER)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
finally:
    codes._hash, codes._check_guesses = real_hash, real_check_guesses
check("eight guesses sent at once still get only five tries",
      len(judged) == codes.MAX_ATTEMPTS, len(judged))

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
except codes.CodeError as exc:
    check("one address cannot be sent endless codes", True)
    check("and is not told that codes were sent, which a failed email was not",
          "were sent" not in str(exc) and "Google" not in str(exc), exc)

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

# Five tries a code, and a new code every few minutes, used to allow well
# over a thousand guesses a day at one inbox. Wrong tries now count across
# every code the address was sent.
gus = "gus@example.com"
for n in range(codes.WRONG_PER_DAY // 4):
    codes.send_email_code(conn, gus, "8.8.%d.1" % n, SECRET, fake_send)
    for _ in range(4):
        try:
            codes.check_email_code(conn, gus, "000000" if sent[gus] != "000000"
                                   else "111111", SECRET)
        except codes.CodeError:
            pass
    # Out of the quarter-hour sending limit, but still the same day.
    conn.execute("UPDATE login_codes SET created_ts=created_ts-1000 WHERE target=?",
                 (gus,))
    conn.commit()
try:
    codes.check_email_code(conn, gus, sent[gus], SECRET)
    check("after twenty wrong tries in a day, even the right code is refused", False)
except codes.CodeError as exc:
    check("after twenty wrong tries in a day, even the right code is refused",
          "Too many wrong codes" in str(exc), exc)
try:
    codes.send_email_code(conn, gus, "8.8.9.1", SECRET, fake_send)
    check("and no more codes are sent to that address that day", False)
except codes.CodeError:
    check("and no more codes are sent to that address that day", True)
conn.execute("UPDATE login_codes SET created_ts=created_ts-86400 WHERE target=?", (gus,))
conn.commit()
codes.send_email_code(conn, gus, "8.8.9.2", SECRET, fake_send)
codes.check_email_code(conn, gus, sent[gus], SECRET)
check("a day later the address can sign in again", True)

now = int(time.time())
conn.execute("INSERT INTO login_codes (channel, target, code_hash, ip, created_ts, "
             "expires_ts) VALUES ('email', 'ancient@example.com', 'x', '9.9.9.9', ?, ?)",
             (now - 31 * 86400, now - 31 * 86400 + 600))
conn.commit()
codes.send_email_code(conn, "recent@example.com", "9.9.9.8", SECRET, fake_send)
check("code requests older than a month are deleted",
      conn.execute("SELECT COUNT(*) FROM login_codes WHERE target='ancient@example.com'")
      .fetchone()[0] == 0)
check("while this month's are kept for the limits",
      conn.execute("SELECT COUNT(*) FROM login_codes WHERE target=?", (gus,))
      .fetchone()[0] > 0)

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



def twilio_says_no(path, data):
    if path == "VerificationCheck":
        raise codes.CodeError("That number could not be sent a code.")
    return {"status": "pending"}


codes.send_phone_code(conn, "+15551230077", "6.6.6.7", twilio_says_no)
try:
    codes.check_phone_code(conn, "+15551230077", "123456", twilio_says_no)
    check("a texted code that cannot be checked lets nobody in", False)
except codes.CodeError as exc:
    check("a texted code that cannot be checked is not blamed on the number",
          "run out or could not be checked" in str(exc) and "number" not in str(exc), exc)
check("nor counted as a wrong guess",
      conn.execute("SELECT SUM(attempts) FROM login_codes WHERE target='+15551230077'")
      .fetchone()[0] == 0)

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
check("with a digit, not the plus, on its picture", first.initial == "1", first.initial)
check("whose placeholder address can never be delivered to",
      first.email.endswith("@users.invalid"))
try:
    A.upsert_email_user(conn, A.LOCAL_EMAIL)
    check("the local account cannot be signed in to by email", False)
except ValueError:
    check("the local account cannot be signed in to by email", True)

codes.send_email_code(conn, "leaving@example.com", "10.0.0.1", SECRET, fake_send)
leaving = A.upsert_email_user(conn, "leaving@example.com")
codes.send_phone_code(conn, "+15550009999", "10.0.0.2", fake_twilio)
texting = A.upsert_phone_user(conn, "+15550009999")
A.delete_user(conn, leaving.id)
A.delete_user(conn, texting.id)
check("deleting an account deletes the sign-in codes sent to it, and from where",
      conn.execute("SELECT COUNT(*) FROM login_codes WHERE target IN "
                   "('leaving@example.com', '+15550009999')").fetchone()[0] == 0)

print()
print("=" * 72)
print("ONE INBOX, MANY ADDRESSES")
print("=" * 72)

from bot import invites  # noqa: E402

check("a +tag reaches the same inbox",
      codes.inbox_of("Me+stocks@Gmail.com") == "me@gmail.com")
check("so do Gmail's dots, at either of Google's domains",
      codes.inbox_of("m.e@googlemail.com") == "me@gmail.com")
check("other providers keep their dots", codes.inbox_of("m.e+x@example.com")
      == "m.e@example.com")

host = A.upsert_email_user(conn, "me@gmail.com")
link = invites.code_for(conn, host.id)
alias = A.upsert_email_user(conn, "me+1@gmail.com")
check("an alias of the inviter's own inbox earns no free week",
      not invites.accept(conn, alias.id, link)
      and A.get_user(conn, host.id).bonus_until == 0
      and A.get_user(conn, alias.id).bonus_until == 0)
dotted = A.upsert_email_user(conn, "m.e+2@gmail.com")
check("nor does one spelled with dots", not invites.accept(conn, dotted.id, link))
friend = A.upsert_email_user(conn, "you@gmail.com")
check("a different person still does", invites.accept(conn, friend.id, link)
      and A.get_user(conn, friend.id).bonus_until > time.time())
again = A.upsert_email_user(conn, "you+again@gmail.com")
check("but a second account for that friend's inbox does not",
      not invites.accept(conn, again.id, link))
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
    twilio_asked = []
    codes._twilio = lambda *a, **k: (twilio_asked.append(a), fake_twilio(*a, **k))[1]
    try:
        r = c.post("/signin/email", data={"to": "Fay@Example.com", "next": "/trades"})
        check("asking for a code moves on to the code page",
              r.status_code == 302 and "/signin/email/code" in r.headers["Location"],
              r.status_code)
        page = c.get("/signin/email/code").data.decode("utf-8", "replace")
        check("which says where it was sent", "fay@example.com" in page)
        r = c.post("/signin/email/code", data={"code": "999999"
                                               if outbox["fay@example.com"] != "999999" else "888888"})
        page = r.data.decode("utf-8", "replace")
        check("a wrong code keeps them on the page with a reason",
              r.status_code == 200 and "not right" in page)
        check("read out, and tied to the box it is about",
              'role="alert" id="code-error"' in page
              and 'aria-describedby="code-error" aria-invalid="true">' in page)
        check("and starting again still goes where they were headed",
              'href="/signin/email?next=/trades"' in page)
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
        check("and tied to the box", 'value="not-an-email" aria-describedby="to-error" '
              'aria-invalid="true">' in bad.data.decode("utf-8", "replace"))
        # A form on another site would mail an address of its choosing.
        for path, to in (("/signin/email", "victim@example.com"),
                         ("/signin/phone", "+1 555 765 9999")):
            sent = len(outbox), len(twilio_asked)
            stranger = web.app.test_client()
            r = stranger.post(path, data={"to": to}, headers={
                "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
            check("a form on another site sends no code by %s" % path[8:],
                  (len(outbox), len(twilio_asked)) == sent
                  and not r.headers.getlist("Set-Cookie")
                  and "here to be sent a code" in r.data.decode("utf-8", "replace"),
                  (r.status_code, r.headers))
            r = stranger.post(path, data={"to": to}, headers={
                "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
            check("while the form here does", r.status_code == 302
                  and (len(outbox), len(twilio_asked)) != sent)
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
print("=" * 72)
print("A SETTING TYPED WRONG")
print("=" * 72)

# A stray letter in the first stopped every start of the site, and in the
# second every code sent by email answered that something had broken.
import subprocess  # noqa: E402

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for typed, used in (("forty", 40), ("", 40), (" ", 40), ("-3", 40), ("12", 12), ("0", 0)):
    ran = subprocess.run(
        [_sys.executable, "-c", "import sys; sys.path.insert(0, %r); "
         "from bot import codes; print(codes.PHONE_PER_HOUR)" % ROOT],
        env=dict(_os.environ, PHONE_CODES_PER_HOUR=typed),
        capture_output=True, text=True, timeout=60)
    check("texts an hour typed as %r are %d" % (typed, used),
          ran.stdout.strip() == str(used), (ran.stdout, ran.stderr[-300:]))
    check("and the log says when it was not usable",
          ("PHONE_CODES_PER_HOUR" in ran.stderr) == (typed in ("forty", "-3")), ran.stderr)

asked = []


class Refuses:
    def __init__(self, host, port, **kw):
        asked.append(port)
        raise OSError("made up")


real_smtp = codes.smtplib.SMTP
codes.smtplib.SMTP = Refuses
_os.environ.update({"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u",
                    "SMTP_PASSWORD": "p", "MAIL_FROM": "hi@example.com"})
try:
    for typed, used in (("tls", 587), ("", 587), ("99999", 587), ("2525", 2525)):
        _os.environ["SMTP_PORT"] = typed
        try:
            codes.send_mail("a@example.com", "s", "b")
        except codes.CodeError:
            pass
        except Exception as exc:
            asked.append("%s: %s" % (type(exc).__name__, exc))
        check("a mail port typed as %r is %d" % (typed, used), asked[-1:] == [used], asked)
finally:
    codes.smtplib.SMTP = real_smtp
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "SMTP_PORT"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("CODES OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
