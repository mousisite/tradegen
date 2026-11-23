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
demand is exactly what SMS-pumping fraud looks for. Wrong guesses are capped
per address per day, across all the codes it was sent, and the record of a
request is deleted after a month.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
import unicodedata
from email.message import EmailMessage
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)


def _number(key: str, default: int, least: int, most: int) -> int:
    """A whole number from the settings, or the default when it is mistyped:
    a stray letter must not stop the site, nor every code it sends."""
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if least <= value <= most:
            return value
    except ValueError:
        pass
    log.warning("%s is %r, which is not a usable number, so %d is used",
                key, raw, default)
    return default


CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
# Wrong codes one address may have typed in a day, across every code sent to
# it. Five tries a code and a fresh code every few minutes otherwise add up to
# more than a thousand guesses a day at one inbox.
WRONG_PER_DAY = 20
# Code requests are kept this long for the limits here, then deleted.
KEEP_DAYS = 30
# Per address: enough to cover a code that went to spam and a retry.
PER_TARGET = (3, 15 * 60)
# Per network address: enough for a household, too few to farm.
PER_IP = (10, 60 * 60)
# Texts cost money per message, and SMS-pumping fraud sends to thousands of
# different numbers from thousands of addresses, which no per-number or
# per-address limit stops. This caps what a bad hour can cost in total.
PHONE_PER_HOUR = _number("PHONE_CODES_PER_HOUR", 40, 0, 1_000_000)

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
# What an address in a To line can carry that is not part of where it goes: a
# comment, a name, a group, quotes. me(1)@gmail.com and x:me@gmail.com both
# reach me@gmail.com, so one inbox could pass as any number of people.
_NOT_ADDRESS = re.compile(r'[()<>\[\]:;,\\"]|=\?')
# Endings reserved never to be a real inbox. The placeholder addresses of
# phone and Apple accounts are under .invalid.
_NEVER_DELIVERED = (".invalid", ".test", ".example", ".localhost")


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
    if not _EMAIL.match(email) or len(email) > 254 \
            or _NOT_ADDRESS.search(email) or not email.isprintable() \
            or unicodedata.normalize("NFKC", email) != email \
            or email.endswith(_NEVER_DELIVERED):
        raise CodeError("That does not look like an email address.")
    return email


# Providers that deliver an address with dots in its name to the same inbox
# as the one without them, mapped to their main domain.
_DOTLESS = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}


def inbox_of(email: str) -> str:
    """The inbox an address delivers to, for telling one person's aliases apart.

    Most providers deliver name+anything@ to name@, and Gmail also ignores
    dots, so me+1@gmail.com and m.e@gmail.com reach the inbox of me@gmail.com.
    The address itself is still what an account is known by.
    """
    email = (email or "").strip().lower()
    local, _, domain = email.rpartition("@")
    if not local:
        return email
    local = local.split("+", 1)[0]
    if domain in _DOTLESS:
        domain = _DOTLESS[domain]
        local = local.replace(".", "")
    return "%s@%s" % (local, domain)


def clean_phone(raw: str) -> str:
    """To E.164: a plus, a country code, and the number, nothing else."""
    digits = re.sub(r"[\s().-]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    # The ten digits themselves: \d also takes every other script's, and the
    # same number typed in those was a different number to the limits here.
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", digits):
        raise CodeError("Type the number with its country code, for example "
                        "+1 555 123 4567.")
    allowed = [p.strip() for p in
               (os.environ.get("PHONE_ALLOWED_PREFIXES") or "").split(",")
               if p.strip()]
    if allowed and not any(digits.startswith(p) for p in allowed):
        raise CodeError("Sign-in by text is not available for that country "
                        "yet. Sign in another way.")
    return digits


# --- limits --------------------------------------------------------------------

def _recent(conn, column: str, value: str, window: int) -> int:
    since = int(time.time()) - window
    return conn.execute(
        "SELECT COUNT(*) FROM login_codes WHERE %s=? AND created_ts>=?" % column,
        (value, since)).fetchone()[0]


def _check_limits(conn, channel: str, target: str, ip: str) -> None:
    count, window = PER_TARGET
    if channel == "email":
        # By the inbox it reaches: counted by the address as typed, me+1@,
        # me+2@ and so on were each allowed their own, all to one person.
        inbox = inbox_of(target)
        asked = sum(1 for row in conn.execute(
            "SELECT target FROM login_codes WHERE channel='email' AND created_ts>=?",
            (int(time.time()) - window,)) if inbox_of(row[0]) == inbox)
    else:
        asked = _recent(conn, "target", target, window)
    if asked >= count:
        raise CodeError("Several codes were asked for just now. Wait a few "
                        "minutes, then try again.")
    count, window = PER_IP
    if ip and _recent(conn, "ip", ip, window) >= count:
        raise CodeError("Too many codes were asked for from this connection. "
                        "Try again in an hour.")


_WRONG_TODAY = ("SELECT COALESCE(SUM(attempts), 0) FROM login_codes WHERE "
                "channel=? AND target=? AND created_ts>=?")


def _today_for(channel: str, target: str) -> Tuple[str, str, int]:
    return channel, target, int(time.time()) - 24 * 60 * 60


def _check_guesses(conn, channel: str, target: str) -> None:
    """Refuse an address once it has had too many wrong codes in a day.

    Counted across codes, because a limit on each code alone is reset by
    asking for a new one.
    """
    wrong = conn.execute(_WRONG_TODAY, _today_for(channel, target)).fetchone()[0]
    if wrong >= WRONG_PER_DAY:
        raise CodeError("Too many wrong codes have been typed for this sign-in "
                        "today. Try again tomorrow, or sign in another way.")


def _count_guess(conn, row_id: int, channel: str, target: str) -> None:
    """Count a guess against its code and its day before it is judged.

    The look at both limits and the count are one statement, so guesses sent
    at the same moment cannot all be judged against the same count. A guess
    that turns out right is taken off again.
    """
    cur = conn.execute(
        "UPDATE login_codes SET attempts=attempts+1 WHERE id=? AND attempts<? "
        "AND (%s)<?" % _WRONG_TODAY,
        (row_id, MAX_ATTEMPTS) + _today_for(channel, target) + (WRONG_PER_DAY,))
    conn.commit()
    if cur.rowcount != 1:
        _check_guesses(conn, channel, target)
        raise CodeError("Too many wrong tries. Ask for a new code.")


def _uncount_guess(conn, row_id: int) -> None:
    conn.execute("UPDATE login_codes SET attempts=attempts-1 WHERE id=? AND "
                 "attempts>0", (row_id,))
    conn.commit()


def _use_up(conn, row_id: int, channel: str, target: str) -> None:
    """A right code: spent, and with it every older one the address was sent,
    which would otherwise open the account again until it ran out."""
    conn.execute("UPDATE login_codes SET attempts=attempts-1 WHERE id=? AND "
                 "attempts>0", (row_id,))
    conn.execute("UPDATE login_codes SET used=1 WHERE channel=? AND target=? AND "
                 "id<=? AND used=0", (channel, target, row_id))
    conn.commit()


def _ask(conn, channel: str, target: str, ip: str, code_hash: str = "") -> None:
    """Write down one code request, if the limits allow it. Raises CodeError.

    The look at the limits and the row they count are one transaction, so
    requests sent at the same moment cannot all be let through by the same
    count.
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _check_limits(conn, channel, target, ip)
        _check_guesses(conn, channel, target)
        if channel == "phone" and _recent(conn, "channel", "phone",
                                          60 * 60) >= PHONE_PER_HOUR:
            raise CodeError("Sign-in by text is busy right now. Sign in another "
                            "way, or try again later.")
        _forget_old(conn)
        now = int(time.time())
        conn.execute(
            "INSERT INTO login_codes (channel, target, code_hash, ip, created_ts, "
            "expires_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (channel, target, code_hash, ip, now, now + CODE_TTL))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _forget_old(conn) -> None:
    """Delete code requests once no limit needs them any more."""
    conn.execute("DELETE FROM login_codes WHERE created_ts < ?",
                 (int(time.time()) - KEEP_DAYS * 24 * 60 * 60,))


# --- email ---------------------------------------------------------------------

def _hash(secret: bytes, target: str, code: str) -> str:
    return hmac.new(secret, ("%s|%s" % (target, code)).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def send_email_code(conn, email: str, ip: str, secret: bytes,
                    sender=None) -> None:
    """Make a code, store its hash, and email it. Raises CodeError."""
    ensure_schema(conn)
    code = "%06d" % secrets.randbelow(10 ** 6)
    _ask(conn, "email", email, ip, _hash(secret, email, code))
    (sender or _smtp_send)(email, code)


def _smtp_send(to: str, code: str) -> None:
    send_mail(to, "Your Orenth code: %s" % code,
              "Your Orenth sign-in code is:\n\n    %s\n\n"
              "It works for ten minutes. If you did not ask for it, ignore this "
              "email: nobody can sign in without the code.\n" % code)


def send_mail(to: str, subject: str, body: str,
              headers: Optional[Dict[str, str]] = None) -> None:
    """One plain-text email through the configured SMTP server."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = to
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = _number("SMTP_PORT", 587, 1, 65535)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=ssl.create_default_context())
        with server:
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            # To exactly this address, not to whatever can be read out of the
            # To line.
            server.send_message(msg, to_addrs=[to])
    except (OSError, smtplib.SMTPException) as exc:
        raise CodeError("The email could not be sent just now. Try again in a "
                        "minute, or sign in another way.") from exc


def check_email_code(conn, email: str, code: str, secret: bytes) -> None:
    """Accept or refuse a code. Raises CodeError; returns nothing on success."""
    ensure_schema(conn)
    _check_guesses(conn, "email", email)
    code = re.sub(r"\D", "", code or "")
    row = conn.execute(
        "SELECT * FROM login_codes WHERE channel='email' AND target=? AND used=0 "
        "ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    if row is None or row["expires_ts"] < time.time():
        raise CodeError("That code has expired. Ask for a new one.")
    _count_guess(conn, row["id"], "email", email)
    if len(code) != 6 or not hmac.compare_digest(
            row["code_hash"], _hash(secret, email, code)):
        left = max(0, MAX_ATTEMPTS - conn.execute(
            "SELECT attempts FROM login_codes WHERE id=?",
            (row["id"],)).fetchone()[0])
        raise CodeError("That code is not right. %d tr%s left."
                        % (left, "y" if left == 1 else "ies"))
    _use_up(conn, row["id"], "email", email)


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
                        "minute, or sign in another way.") from exc
    if r.status_code >= 400:
        raise CodeError("That number could not be sent a code. Check it, or "
                        "sign in another way.")
    return r.json()


def send_phone_code(conn, phone: str, ip: str, poster=None) -> None:
    ensure_schema(conn)
    # A row with no code, only so the limits can count the request.
    _ask(conn, "phone", phone, ip)
    (poster or _twilio)("Verifications", {"To": phone, "Channel": "sms"})


def check_phone_code(conn, phone: str, code: str, poster=None) -> None:
    ensure_schema(conn)
    _check_guesses(conn, "phone", phone)
    code = re.sub(r"\D", "", code or "")
    row = conn.execute(
        "SELECT * FROM login_codes WHERE channel='phone' AND target=? AND used=0 "
        "ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
    if row is None or row["expires_ts"] < time.time():
        raise CodeError("That code has expired. Ask for a new one.")
    _count_guess(conn, row["id"], "phone", phone)
    try:
        result = (poster or _twilio)("VerificationCheck",
                                     {"To": phone, "Code": code})
    except CodeError as exc:
        # Nothing was judged, so it was not a wrong guess. Twilio's own errors
        # are about sending a text, which is not what went wrong here.
        _uncount_guess(conn, row["id"])
        raise CodeError("That code has run out or could not be checked. Try it "
                        "once more, or ask for a new one.") from exc
    if result.get("status") != "approved":
        raise CodeError("That code is not right.")
    _use_up(conn, row["id"], "phone", phone)
