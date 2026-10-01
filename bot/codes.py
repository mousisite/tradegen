"""Sign-in by a one-time code sent to an email address or a phone.

No passwords. A person types their address, is sent six digits, types them
back, and is in. Nothing reusable is stored: an email code is kept only as a
keyed hash, for ten minutes, for five attempts. A phone code never touches
this server at all; Twilio Verify generates it, sends it and checks it.

Both are switched on only by configuration, because both need an outside
service to deliver anything:

  Email   SMTP_HOST, SMTP_USER, SMTP_PASSWORD, MAIL_FROM  (SMTP_PORT, 587)
          Works with Resend, Brevo, Postmark, or a Gmail app password.
  Phone   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VERIFY_SID
          PHONE_ALLOWED_PREFIXES, optional: "+1,+44" limits the countries.

Sending is limited per address and per network address. A code request
costs real money for a text message, and an open form that sends texts on
demand is exactly what SMS-pumping fraud looks for.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage
from typing import Optional, Tuple

CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
# Per address: enough to cover a code that went to spam and a retry.
PER_TARGET = (3, 15 * 60)
# Per network address: enough for a household, too few to farm.
PER_IP = (10, 60 * 60)
# Texts cost money per message, and SMS-pumping fraud sends to thousands of
# different numbers from thousands of addresses, which no per-number or
# per-address limit stops. This caps what a bad hour can cost in total.
PHONE_PER_HOUR = int(os.environ.get("PHONE_CODES_PER_HOUR") or 40)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    code_hash   TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    created_ts  INTEGER NOT NULL,
    expires_ts  INTEGER NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_login_codes_target ON login_codes(channel, target);
"""

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


class CodeError(ValueError):
    """Something the person can fix, phrased for them."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# --- what is switched on -------------------------------------------------------

def email_ready() -> bool:
    return all((os.environ.get(k) or "").strip()
               for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"))


def phone_ready() -> bool:
    return all((os.environ.get(k) or "").strip()
               for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                         "TWILIO_VERIFY_SID"))


# --- cleaning what people type -------------------------------------------------

def clean_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise CodeError("That does not look like an email address.")
    return email


def clean_phone(raw: str) -> str:
    """To E.164: a plus, a country code, and the number, nothing else."""
    digits = re.sub(r"[\s().-]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not re.match(r"^\+[1-9]\d{7,14}$", digits):
        raise CodeError("Type the number with its country code, for example "
                        "+1 555 123 4567.")
    allowed = [p.strip() for p in
               (os.environ.get("PHONE_ALLOWED_PREFIXES") or "").split(",")
               if p.strip()]
    if allowed and not any(digits.startswith(p) for p in allowed):
        raise CodeError("Sign-in by text is not available for that country "
                        "yet. Email or Google will work.")
    return digits


# --- limits --------------------------------------------------------------------

def _recent(conn, column: str, value: str, window: int) -> int:
    since = int(time.time()) - window
    return conn.execute(
        "SELECT COUNT(*) FROM login_codes WHERE %s=? AND created_ts>=?" % column,
        (value, since)).fetchone()[0]


def _check_limits(conn, target: str, ip: str) -> None:
    count, window = PER_TARGET
    if _recent(conn, "target", target, window) >= count:
        raise CodeError("Several codes were sent to that address just now. "
                        "Wait a few minutes, then try again.")
    count, window = PER_IP
    if ip and _recent(conn, "ip", ip, window) >= count:
        raise CodeError("Too many codes were asked for from this connection. "
                        "Try again in an hour.")


# --- email ---------------------------------------------------------------------

def _hash(secret: bytes, target: str, code: str) -> str:
    return hmac.new(secret, ("%s|%s" % (target, code)).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def send_email_code(conn, email: str, ip: str, secret: bytes,
                    sender=None) -> None:
    """Make a code, store its hash, and email it. Raises CodeError."""
    ensure_schema(conn)
    _check_limits(conn, email, ip)
    code = "%06d" % secrets.randbelow(10 ** 6)
    now = int(time.time())
    conn.execute(
        "INSERT INTO login_codes (channel, target, code_hash, ip, created_ts, "
        "expires_ts) VALUES ('email', ?, ?, ?, ?, ?)",
        (email, _hash(secret, email, code), ip, now, now + CODE_TTL))
    conn.commit()
    (sender or _smtp_send)(email, code)


def _smtp_send(to: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your Orenth code: %s" % code
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = to
    msg.set_content(
        "Your Orenth sign-in code is:\n\n    %s\n\n"
        "It works for ten minutes. If you did not ask for it, ignore this "
        "email: nobody can sign in without the code.\n" % code)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=ssl.create_default_context())
        with server:
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise CodeError("The email could not be sent just now. Try again in a "
                        "minute, or use Google.") from exc


def check_email_code(conn, email: str, code: str, secret: bytes) -> None:
    """Accept or refuse a code. Raises CodeError; returns nothing on success."""
    ensure_schema(conn)
    code = re.sub(r"\D", "", code or "")
    row = conn.execute(
        "SELECT * FROM login_codes WHERE channel='email' AND target=? AND used=0 "
        "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    if row is None or row["expires_ts"] < time.time():
        raise CodeError("That code has expired. Ask for a new one.")
    if row["attempts"] >= MAX_ATTEMPTS:
        raise CodeError("Too many wrong tries. Ask for a new code.")
    if len(code) != 6 or not hmac.compare_digest(
            row["code_hash"], _hash(secret, email, code)):
        conn.execute("UPDATE login_codes SET attempts=attempts+1 WHERE id=?",
                     (row["id"],))
        conn.commit()
        left = MAX_ATTEMPTS - row["attempts"] - 1
        raise CodeError("That code is not right. %d tr%s left."
                        % (left, "y" if left == 1 else "ies"))
    conn.execute("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
    conn.commit()


# --- phone, through Twilio Verify ----------------------------------------------

def _twilio(path: str, data: dict) -> dict:
    import requests
    sid = os.environ["TWILIO_VERIFY_SID"]
    url = "https://verify.twilio.com/v2/Services/%s/%s" % (sid, path)
    try:
        r = requests.post(url, data=data, timeout=15,
                          auth=(os.environ["TWILIO_ACCOUNT_SID"],
                                os.environ["TWILIO_AUTH_TOKEN"]))
    except requests.RequestException as exc:
        raise CodeError("The text could not be sent just now. Try again in a "
                        "minute, or use Google.") from exc
    if r.status_code >= 400:
        raise CodeError("That number could not be sent a code. Check it, or "
                        "use email or Google instead.")
    return r.json()


def send_phone_code(conn, phone: str, ip: str, poster=None) -> None:
    ensure_schema(conn)
    _check_limits(conn, phone, ip)
    if _recent(conn, "channel", "phone", 60 * 60) >= PHONE_PER_HOUR:
        raise CodeError("Sign-in by text is busy right now. Use email or "
                        "Google, or try again later.")
    now = int(time.time())
    # A row with no code, only so the limits above can count the request.
    conn.execute(
        "INSERT INTO login_codes (channel, target, ip, created_ts, expires_ts) "
        "VALUES ('phone', ?, ?, ?, ?)", (phone, ip, now, now + CODE_TTL))
    conn.commit()
    (poster or _twilio)("Verifications", {"To": phone, "Channel": "sms"})


def check_phone_code(conn, phone: str, code: str, poster=None) -> None:
    ensure_schema(conn)
    code = re.sub(r"\D", "", code or "")
    row = conn.execute(
        "SELECT * FROM login_codes WHERE channel='phone' AND target=? AND used=0 "
        "ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
    if row is None or row["expires_ts"] < time.time():
        raise CodeError("That code has expired. Ask for a new one.")
    if row["attempts"] >= MAX_ATTEMPTS:
        raise CodeError("Too many wrong tries. Ask for a new code.")
    result = (poster or _twilio)("VerificationCheck", {"To": phone, "Code": code})
    if result.get("status") != "approved":
        conn.execute("UPDATE login_codes SET attempts=attempts+1 WHERE id=?",
                     (row["id"],))
        conn.commit()
        raise CodeError("That code is not right.")
    conn.execute("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
    conn.commit()
