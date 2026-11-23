"""Notifications to phones and browsers, and Orenth as an installable app.

Web Push encryption against RFC 8291's own worked example, the VAPID and APNs
tokens, Apple's environments and refusals against a stubbed HTTP/2 client,
devices that die or change hands, what the endpoints refuse, delivery from a
fired alert, the manifest, the service worker, the offline page and the
icons. No network: every push service is a stand-in.
"""
import json
import logging
import os as _os
import re
import shutil
import struct
import subprocess
import sys as _sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import harness

fails = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


for key in ("STOCKBOT_ADMIN_EMAIL", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
            "MAIL_FROM", "STOCKBOT_CONTACT", "STOCKBOT_PUBLIC_URL",
            "VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "APPLE_TEAM_ID", "APNS_KEY_ID",
            "APNS_PRIVATE_KEY", "APNS_BUNDLE_ID"):
    _os.environ.pop(key, None)
_os.environ["GOOGLE_CLIENT_ID"] = "1.apps.googleusercontent.com"
_os.environ["GOOGLE_CLIENT_SECRET"] = "x"

import httpx2  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from PIL import Image  # noqa: E402

import web  # noqa: E402
from bot import accounts as A  # noqa: E402
from bot import alerts  # noqa: E402
from bot import database as db  # noqa: E402
from bot import push  # noqa: E402
from bot import webapp  # noqa: E402

c, DB = harness.isolated_web("push")
web.app.config["AUTH_MODE"] = None
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def b(text):
    return push.unb64url(text.replace(" ", ""))


def person(email, name=""):
    conn = db.connect(DB)
    try:
        user = A.upsert_email_user(conn, email)
        if name:
            conn.execute("UPDATE users SET name=? WHERE id=?", (name, user.id))
            conn.commit()
        return user
    finally:
        conn.close()


def sign_in(user, client=c):
    conn = db.connect(DB)
    try:
        token = A.start_session(conn, user.id, "test")
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess[web.SESSION_KEY] = token
    return token


def sign_out(client=c):
    with client.session_transaction() as sess:
        sess.clear()


def browser():
    """A browser's keys and the subscription it would hand a page."""
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    secret = _os.urandom(16)
    return key, secret, {"keys": {"p256dh": push.b64url(raw), "auth": push.b64url(secret)}}


def subscription(endpoint, keys=None):
    sub = dict(keys or browser()[2])
    sub["endpoint"] = endpoint
    return sub


def decrypt(body, ua_key, auth):
    """What a browser does with a notice: the receiving side of RFC 8291."""
    salt, (size, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    ua_public = ua_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_public))
    cek, nonce = push.content_keys(secret, auth, ua_public, as_public, salt)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None).rstrip(b"\x00")
    assert plain.endswith(b"\x02") and size == 4096
    return plain[:-1]


def jwt_parts(token):
    head, claims, sig = token.split(".")
    return json.loads(b(head)), json.loads(b(claims)), b(sig)


def verifies(token, public_key):
    head, claims, sig = token.split(".")
    raw = b(sig)
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"),
                               int.from_bytes(raw[32:], "big"))
    try:
        public_key.verify(der, ("%s.%s" % (head, claims)).encode("ascii"),
                          ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


class Recorder:
    """A stand-in push service for browsers: records each POST, answers status."""

    def __init__(self, status=201, delay=0.0):
        self.status, self.delay, self.calls = status, delay, []

    def __call__(self, url, body, headers):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append((url, body, dict(headers)))
        status = self.status(url) if callable(self.status) else self.status
        if isinstance(status, Exception):
            raise status
        return status


class Apple:
    """A stand-in for Apple's push service, behind httpx2's own mock transport."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []
        self.client = httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        status, reason = self.answer(request)
        return httpx2.Response(status, json={"reason": reason} if reason else None)


def use_apple(answer):
    apple = Apple(answer)
    push._CLIENT["apns"] = apple.client
    push._APNS_PAUSED["until"] = 0.0
    return apple


def device_rows(user_id):
    conn = db.connect(DB)
    try:
        return push.devices(conn, user_id)
    finally:
        conn.close()


NODE = shutil.which("node")


def run_node(script, *args):
    """Run a script under Node and return the JSON it printed last.

    The page scripts and the service workers are run for real where Node is
    installed. Returns None without it, and {"error": ...} if the script
    itself failed.
    """
    if not NODE:
        return None
    fd, path = tempfile.mkstemp(suffix=".js", prefix="orenth-test-")
    with _os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        proc = subprocess.run([NODE, path] + list(args), capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
    finally:
        _os.remove(path)
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not lines:
        return {"error": (proc.stderr or proc.stdout or "no output")[-1500:]}
    return json.loads(lines[-1])


# static/notify.js inside the iPhone app, with stand-ins for Capacitor's push
# plugin, fetch, the page's storage and one notifications card.
NOTIFY_IN_APP = r"""
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const TOKEN = 'ab'.repeat(32);
const OWNER = 'orenth-push-owner', KEPT = 'orenth-apns-token';

function store(init) {
  const m = new Map(Object.entries(init || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null),
           setItem: (k, v) => { m.set(k, String(v)); },
           removeItem: (k) => { m.delete(k); }, dump: () => Object.fromEntries(m) };
}
function element() {
  const on = {};
  return { hidden: true, disabled: false, textContent: '', className: '',
           addEventListener: (type, fn) => { on[type] = fn; },
           click: () => { if (on.click) { on.click(); } } };
}
// One page in the app, signed in as `user`; net(url, body) answers each request.
function page(user, local, net, permission) {
  const calls = [];
  const parts = {};
  for (const sel of ['[data-notify-state]', '[data-notify-said]', '[data-notify-act="on"]',
                     '[data-notify-act="off"]', '[data-notify-act="test"]']) {
    parts[sel] = element();
  }
  const card = { hidden: true, querySelector: (sel) => parts[sel] || null };
  const push = {
    addListener: (ev, fn) => { push['_' + ev] = fn; return Promise.resolve({ remove() {} }); },
    register: () => {
      setTimeout(() => push._registration && push._registration({ value: TOKEN }), 0);
      return Promise.resolve();
    },
    checkPermissions: () => Promise.resolve({ receive: permission || 'granted' }),
    requestPermissions: () => Promise.resolve({ receive: permission || 'granted' }),
  };
  const ctx = {
    console, setTimeout, Promise, JSON, Uint8Array, URL, Error,
    localStorage: local, sessionStorage: store(),
    Capacitor: { isNativePlatform: () => true, Plugins: { PushNotifications: push } },
    navigator: { userAgent: 'Mozilla/5.0 (iPhone) OrenthApp/1' },
    location: { origin: 'https://orenth.app' },
    document: {
      readyState: 'complete',
      currentScript: { getAttribute: (a) => ({ 'data-user': user, 'data-mark': 'm' + user })[a] },
      querySelectorAll: (sel) => (sel === '[data-notify]' ? [card] : []),
      addEventListener() {},
    },
    fetch: (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      calls.push({ url: url, body: body });
      return net(url, body);
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { calls: calls, chip: parts['[data-notify-state]'], said: parts['[data-notify-said]'],
           off: parts['[data-notify-act="off"]'] };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 40));
const answer = (status, body) => Promise.resolve({
  ok: status >= 200 && status < 300, status: status, json: () => Promise.resolve(body) });
const fine = (url) => answer(200, url.indexOf('/config') !== -1
  ? { web: { enabled: false }, apns: { enabled: true } } : { ok: true, known: true, removed: 1 });
const FAILURES = {
  'no connection': () => Promise.reject(new TypeError('Load failed.')),
  'a 502 during a deploy': () => answer(502, {}),
  'a 429': () => answer(429, { error: 'Wait a while.' }),
};
const released = (calls) => calls.some((c) => c.url.indexOf('/unsubscribe') !== -1
  && c.body && c.body.release === true && c.body.token === TOKEN);

(async () => {
  const out = { handover: {} };
  // Someone else's phone: account 1 turned notifications on, account 2 signs in.
  for (const name of Object.keys(FAILURES)) {
    const local = store({ [OWNER]: '1', [KEPT]: TOKEN });
    const first = page('2', local, FAILURES[name]);
    await settle();
    const kept = local.dump();
    const next = page('2', local, fine);
    await settle();
    out.handover[name] = { asked: released(first.calls), kept: kept,
                           asked_again: released(next.calls), after: local.dump() };
  }
  // Turning them off while the connection drops, then once it is back.
  {
    const local = store({ [OWNER]: '2', [KEPT]: TOKEN });
    let net = fine;
    const p = page('2', local, (url, body) => net(url, body));
    await settle();
    const before = p.chip.textContent;
    net = FAILURES['no connection'];
    p.off.click();
    await settle();
    const failed = { chip: p.chip.textContent, said: p.said.textContent, kept: local.dump() };
    net = fine;
    p.calls.length = 0;
    p.off.click();
    await settle();
    out.turn_off = { before: before, failed: failed,
                     then: { chip: p.chip.textContent, said: p.said.textContent,
                             kept: local.dump(),
                             told: p.calls.some((c) => c.url.indexOf('/unsubscribe') !== -1
                                                && c.body && c.body.token === TOKEN) } };
  }
  // Switched off in the iPhone's Settings, and the server cannot be told.
  {
    const local = store({ [OWNER]: '2', [KEPT]: TOKEN });
    page('2', local, FAILURES['no connection'], 'denied');
    await settle();
    out.revoked = local.dump();
  }
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || String(err)); process.exit(1); });
"""

# static/notify.js inside the iPhone app, on https://orenth.app/monitor and
# signed out, with a stand-in for Capacitor's push plugin. Each address in
# __PATHS__ arrives as a tapped notification's url, on a page of its own; it
# prints where each one sent the page (location.assign), if anywhere.
NOTIFY_TAPPED = r"""
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const PATHS = __PATHS__;

function page() {
  const went = [];
  const heard = {};
  const push = { addListener: (event, fn) => { heard[event] = fn; return Promise.resolve({ remove() {} }); } };
  const ctx = {
    console, Promise, URL, JSON, setTimeout,
    Capacitor: { isNativePlatform: () => true, Plugins: { PushNotifications: push } },
    navigator: { userAgent: 'Mozilla/5.0 (iPhone) OrenthApp/1' },
    location: { origin: 'https://orenth.app', href: 'https://orenth.app/monitor',
                assign: (to) => { went.push(String(to)); } },
    document: { readyState: 'complete', currentScript: { getAttribute: () => '' },
                querySelectorAll: () => [], addEventListener() {} },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { went: went, heard: heard };
}

const out = {};
for (const path of PATHS) {
  const p = page();
  if (!p.heard.pushNotificationActionPerformed) { throw new Error('no listener for a tapped notification'); }
  p.heard.pushNotificationActionPerformed({ notification: { data: { url: path } } });
  out[path] = p.went;
}
console.log(JSON.stringify(out));
"""

# Addresses that must never take the app's web view to another site. Those
# with a dot segment ("/./", "/%2e/", "/../") resolve to a path that starts
# "//", which the browser, given that path alone, reads as the address of
# another site.
OFF_SITE = ["/.//orenth.app.evil.example/x", "/%2e//orenth.app.evil.example/x",
            "/%2E//orenth.app.evil.example/x", "/..//orenth.app.evil.example/x",
            "/.//orenth.app@evil.example/x", "//orenth.app.evil.example/x",
            "/\\orenth.app.evil.example/x", "/\\/orenth.app.evil.example/x",
            "/\t/orenth.app.evil.example/x",
            "javascript:alert(document.cookie)", "data:text/html,<script>alert(1)</script>",
            "https://orenth.app.evil.example/x", "http://orenth.app/monitor"]
# Odd, but a path on this site all the same: "%2F" is not a slash in a path,
# so this asks orenth.app for a page it does not have. In an app link the
# address is decoded once more first, and there it is "/.//orenth.app@...".
ODD_BUT_HERE = "/.%2F/orenth.app@evil.example/x"

# A service worker (argv[2]) in a stand-in for the browser's worker scope:
# its caches, the network, the open windows and its registration.
# argv[3] is 'worker' for /sw.js, with argv[4] the release's version, or
# 'retire' for static/sw-retire.js.
WORKER_SCOPE = r"""
const fs = require('fs');
const vm = require('vm');
const ORIGIN = 'https://orenth.app';
const SRC = fs.readFileSync(process.argv[2], 'utf8');
const MODE = process.argv[3];
const VERSION = process.argv[4] || '';

const address = (r) => new URL(typeof r === 'string' ? r : r.url, ORIGIN).href;
class FakeRequest {
  constructor(url, init) {
    this.url = address(url);
    this.method = (init && init.method) || 'GET';
    this.mode = (init && init.mode) || 'no-cors';
  }
}
const network = [];
async function fakeFetch(request) {
  const url = address(request);
  network.push(url);
  const type = url.indexOf('/static/') !== -1 ? 'text/css' : 'text/html';
  return { ok: true, status: 200, type: 'basic', url: url, from: 'network',
           headers: { get: (h) => (h.toLowerCase() === 'content-type' ? type : null) },
           clone() { return Object.assign({}, this); } };
}
const stores = new Map();
function cacheNamed(name) {
  if (!stores.has(name)) { stores.set(name, new Map()); }
  const kept = stores.get(name);
  return {
    match: async (r) => (kept.has(address(r)) ? Object.assign({}, kept.get(address(r)),
                                                               { from: 'kept' }) : undefined),
    put: async (r, response) => { kept.set(address(r), response); },
    addAll: async (list) => { for (const r of list) { kept.set(address(r), await fakeFetch(r)); } },
  };
}
const caches = { open: async (name) => cacheNamed(name), keys: async () => [...stores.keys()],
                 delete: async (name) => stores.delete(name) };
const listeners = {};
const seen = { skipWaiting: 0, unregistered: 0, navigated: [] };
const windows = ['/monitor', '/trades'].map((path) => ({
  url: ORIGIN + path, navigate: async (to) => { seen.navigated.push(to); return null; } }));
const self = {
  location: { origin: ORIGIN },
  addEventListener: (type, fn) => { listeners[type] = fn; },
  skipWaiting: async () => { seen.skipWaiting += 1; },
  registration: { navigationPreload: null, pushManager: {}, showNotification: async () => {},
                  unregister: async () => { seen.unregistered += 1; return true; } },
  clients: { claim: async () => {}, matchAll: async () => windows, openWindow: async () => null },
};
const ctx = { self: self, caches: caches, fetch: fakeFetch, Request: FakeRequest,
              Response: { error: () => ({ from: 'error' }) }, URL: URL, console: console,
              Promise: Promise, atob: atob, Uint8Array: Uint8Array, JSON: JSON };
vm.createContext(ctx);
vm.runInContext(SRC, ctx);

async function dispatch(type) {
  const waits = [];
  if (listeners[type]) { listeners[type]({ waitUntil: (p) => { waits.push(p); } }); }
  await Promise.all(waits);
}
async function ask(url, mode) {
  let answer = null;
  const event = { request: new FakeRequest(url, { mode: mode }),
                  preloadResponse: Promise.resolve(undefined),
                  respondWith: (p) => { answer = p; } };
  if (listeners.fetch) { listeners.fetch(event); }
  const response = answer ? await answer : null;
  return { answered: !!answer, from: response ? response.from : null };
}

(async () => {
  const out = {};
  if (MODE === 'worker') {
    await dispatch('install');
    await dispatch('activate');
    const before = network.length;
    out.current = await ask('/static/style.css?v=' + VERSION);
    out.current_fetched = network.slice(before).length > 0;
    out.older = await ask('/static/style.css');
    out.another = await ask('/static/style.css?v=000000000000');
    out.page = await ask('/monitor', 'navigate');
    out.api = await ask('/api/push/config');
  } else {
    for (const name of ['orenth-0123456789ab', 'orenth-ba9876543210', 'another-site']) {
      stores.set(name, new Map());
    }
    await dispatch('install');
    await dispatch('activate');
    out.skip_waiting = seen.skipWaiting;
    out.caches = [...stores.keys()];
    out.unregistered = seen.unregistered;
    out.navigated = seen.navigated;
    out.listens = Object.keys(listeners).sort();
  }
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || String(err)); process.exit(1); });
"""

class Logged(logging.Handler):
    """What bot/push.py writes to the server log, where its codes belong."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


LOGGED = Logged()
logging.getLogger("bot.push").addHandler(LOGGED)


SITE = "https://orenth.app"
FCM = "https://fcm.googleapis.com/fcm/send/"
REAL_WEB_POST = push._web_post
REAL_HISTORY = web.market_mod.split_history
REAL_CALL_NOW = web._call_now
REAL_FETCH = alerts.fetch_bars

# RFC 8291, Appendix A, with the whitespace taken out.
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AS_PUBLIC = ("BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8w"
             "EqKK6PBru3jl7A8")
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
UA_PUBLIC = ("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZ"
             "GH6SRpkNtoIAiw4")
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
ECDH_SECRET = "kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs"
PRK_KEY = "Snr3JMxaHVDXHWJn5wdC52WjpCtd2EIEGBykDcZW32k"
KEY_INFO = ("V2ViUHVzaDogaW5mbwAEJXGyvs3942BVGq8e0PTNNmwR zr5VX4m8t7GGpTM5FzFo7OLr4BhZe9MEebhuPI-OztV3"
            "ylkYfpJGmQ22ggCLDgT-M_SrDepxkU21WCP3O1SUj0Ew bZIHMtu5pZpTKGSCIA5Zent7wmC6HCJ5mFgJkuk5cwAv"
            " MBKiiujwa7t45ewP")
IKM = "S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg"
PRK = "09_eUZGrsvxChDCGRCdkLiDXrReGOEVeSCdCcPBSJSc"
CEK = "oIhVW04MRdy2XN9CiKLxTg"
NONCE = "4h_95klXJ5E_qnoN"
HEADER = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z 9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
          "mlMoZIIgDll6e3vCYLocInmYWAmS6Tlz AC8wEqKK6PBru3jl7A8")
CIPHERTEXT = ("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrd irN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs"
              "bI_0LpXMuGvnzQ")
BODY = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

try:
    # Nothing in this suite may reach a real push service, whatever a check
    # forgets to stub: browsers' and Apple's are stand-ins from the start.
    push._web_post = Recorder(201)
    use_apple(lambda request: (200, ""))

    section("ENCRYPTING FOR ONE BROWSER: RFC 8291'S OWN EXAMPLE")
    server = push.private_key(AS_PRIVATE)
    check("the example's server key gives the example's public key",
          push.b64url(push._raw_public(server)) == AS_PUBLIC)
    ua_private = push.private_key(UA_PRIVATE)
    check("and the browser's", push.b64url(push._raw_public(ua_private)) == UA_PUBLIC)
    ua_point = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b(UA_PUBLIC))
    secret = server.exchange(ec.ECDH(), ua_point)
    check("the shared ECDH secret", push.b64url(secret) == ECDH_SECRET)
    prk_key = push._hmac(b(AUTH), secret)
    check("the key-combining PRK", push.b64url(prk_key) == PRK_KEY)
    check("the key_info", b"WebPush: info\x00" + b(UA_PUBLIC) + b(AS_PUBLIC) == b(KEY_INFO))
    ikm = push._hmac(prk_key, b(KEY_INFO) + b"\x01")
    check("the input keying material", push.b64url(ikm) == IKM)
    check("the content PRK", push.b64url(push._hmac(b(SALT), ikm)) == PRK)
    cek, nonce = push.content_keys(secret, b(AUTH), b(UA_PUBLIC), b(AS_PUBLIC), b(SALT))
    check("the content encryption key", push.b64url(cek) == CEK, push.b64url(cek))
    check("the nonce", push.b64url(nonce) == NONCE, push.b64url(nonce))
    body = push.encrypt(PLAINTEXT, b(UA_PUBLIC), b(AUTH), salt=b(SALT), server_key=server)
    check("the 86-octet header", body[:86] == b(HEADER))
    check("the ciphertext", body[86:] == b(CIPHERTEXT))
    check("the whole message, byte for byte", body == b(BODY), push.b64url(body))
    check("which the browser can read back", decrypt(body, ua_private, b(AUTH)) == PLAINTEXT)

    ua_key, ua_auth, _ = browser()
    ua_raw = push._raw_public(ua_key)
    one = push.encrypt(b"same words", ua_raw, ua_auth)
    two = push.encrypt(b"same words", ua_raw, ua_auth)
    check("every message gets its own salt and key", one[:16] != two[:16]
          and one[21:86] != two[21:86])
    check("and each still reads back", decrypt(one, ua_key, ua_auth) == b"same words"
          and decrypt(two, ua_key, ua_auth) == b"same words")
    try:
        push.encrypt(b"x" * 5000, ua_raw, ua_auth)
        check("a notice too long for one record is refused", False)
    except ValueError:
        check("a notice too long for one record is refused", True)

    section("THE SERVER'S VAPID KEY")
    keys = push.vapid_keys()
    kept = push.key_path()
    check("made with no setup at all", keys is not None and len(b(keys.public)) == 65)
    check("and kept beside the database", _os.path.exists(kept)
          and _os.path.dirname(kept) == _os.path.dirname(DB), kept)
    if _os.name != "nt":
        check("readable by the server alone", (_os.stat(kept).st_mode & 0o777) == 0o600)
    push._VAPID.clear()
    check("the same key after a restart", push.vapid_keys().public == keys.public)
    # A start-up that stopped between making the key file and writing it.
    push._VAPID.clear()
    _os.replace(kept, kept + ".kept")
    open(kept, "wb").close()
    check("an empty key file still being written is waited for, never replaced",
          push.vapid_keys() is None and "still being written" in push.why_not()["web"])
    stale = time.time() - 3600
    _os.utime(kept, (stale, stale))
    check("an empty one a stopped start-up left behind is made again",
          push.vapid_keys() is not None and _os.path.getsize(kept) > 0)
    _os.replace(kept + ".kept", kept)
    push._VAPID.clear()
    check("and the kept key is the one in use again", push.vapid_keys().public == keys.public)
    _os.environ["VAPID_PRIVATE_KEY"] = AS_PRIVATE
    check("VAPID_PRIVATE_KEY wins over the kept one, in base64url",
          push.vapid_keys().public == AS_PUBLIC)
    pem = server.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode("ascii")
    _os.environ["VAPID_PRIVATE_KEY"] = pem.strip().replace("\n", "\\n")
    check("or as PEM pasted with \\n for line breaks", push.vapid_keys().public == AS_PUBLIC)
    _os.environ["VAPID_PRIVATE_KEY"] = "not a key"
    check("a setting that is not a key switches browser notices off",
          push.vapid_keys() is None and "VAPID_PRIVATE_KEY" in push.why_not()["web"])
    sign_in(person("owner-check@example.com"))
    got = c.get("/api/push/config").get_json()
    check("and the page is told they are off, without the reason, which is "
          "for whoever runs the server", got["web"] == {"enabled": False, "public_key": ""},
          got)
    _os.environ.pop("VAPID_PRIVATE_KEY")
    check("without it, the kept key is back", push.vapid_keys().public == keys.public)
    got = c.get("/api/push/config").get_json()
    check("the page is given the public key to subscribe with",
          got["web"] == {"enabled": True, "public_key": keys.public}, got)
    check("and told the iPhone app's notifications are off with no settings",
          got["apns"] == {"enabled": False})

    section("A VAPID TOKEN (RFC 8292)")
    _os.environ["STOCKBOT_CONTACT"] = "ops@example.com"
    now = time.time()
    header = push.vapid_header(FCM + "abc", keys, push.subject(SITE), now=now)
    found = re.match(r"^vapid t=([^,]+), k=([A-Za-z0-9_-]+)$", header)
    check("the Authorization header is vapid t=..., k=...", bool(found), header[:40])
    head, claims, sig = jwt_parts(found.group(1))
    check("signed with ES256", head == {"typ": "JWT", "alg": "ES256"}, head)
    check("for the push service's origin", claims["aud"] == "https://fcm.googleapis.com",
          claims)
    check("expiring within 12 hours", 0 < claims["exp"] - int(now) <= 12 * 3600)
    check("naming STOCKBOT_CONTACT to be reached at", claims["sub"] == "mailto:ops@example.com")
    check("with the signature as 64 raw bytes, not DER", len(sig) == 64)
    check("which the public key verifies", verifies(found.group(1), keys.private.public_key()))
    check("and k is the key the browser subscribed with", found.group(2) == keys.public)
    _os.environ.pop("STOCKBOT_CONTACT")
    check("with no contact set, the site's own address", push.subject(SITE) == SITE)

    section("WHICH ADDRESSES A PAGE MAY HAND OVER")
    for url, ok in (("https://fcm.googleapis.com/fcm/send/x", True),
                    ("https://android.googleapis.com/gcm/send/x", True),
                    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
                    ("https://web.push.apple.com/QGx", True),
                    ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
                    # Only Microsoft's is spread over hosts of its own.
                    ("https://x.push.apple.com/abc", False),
                    ("https://h7.fcm.googleapis.com/x", False),
                    ("https://other.push.services.mozilla.com/x", False),
                    ("http://fcm.googleapis.com/fcm/send/x", False),
                    ("https://fcm.googleapis.com:8443/x", False),
                    ("https://fcm.googleapis.com@evil.example/x", False),
                    ("https://evil.example/fcm.googleapis.com", False),
                    ("https://evilfcm.googleapis.com.evil.example/x", False),
                    ("https://127.0.0.1/x", False), ("https://localhost/x", False),
                    ("https://metadata.google.internal/x", False)):
        check("%s %s" % ("takes" if ok else "refuses", url), push.push_service(url) == ok)

    section("THE IPHONE APP: APPLE'S TOKEN")
    apns_key = ec.generate_private_key(ec.SECP256R1())
    p8 = apns_key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    _os.environ.update({"APPLE_TEAM_ID": "TEAM123456", "APNS_KEY_ID": "KEY1234567",
                        "APNS_PRIVATE_KEY": p8.strip().replace("\n", "\\n")})
    check("on once the team, the key's id and the key are set, the key pasted "
          "on one line", push.apns_ready())
    check("and the page is told", c.get("/api/push/config").get_json()["apns"]["enabled"])
    t0 = 1_800_000_000.0
    push._PROVIDER.update({"token": "", "at": 0.0, "for": ()})
    first = push.provider_token(now=t0)
    head, claims, sig = jwt_parts(first)
    check("signed ES256 with the key's id", head == {"alg": "ES256", "kid": "KEY1234567"}, head)
    check("issued by the team, with the time", claims == {"iss": "TEAM123456", "iat": int(t0)},
          claims)
    check("which the key verifies", verifies(first, apns_key.public_key()))
    check("reused for 40 minutes", push.provider_token(now=t0 + 39 * 60) == first)
    second = push.provider_token(now=t0 + 41 * 60)
    check("and made again after them", second != first
          and jwt_parts(second)[1]["iat"] == int(t0 + 41 * 60))
    check("never renewed within 20 minutes, even when Apple calls it expired",
          push.provider_token(now=t0 + 41 * 60 + 5 * 60, renew=True) == second)
    check("but renewed after them when Apple does",
          push.provider_token(now=t0 + 41 * 60 + 21 * 60, renew=True) != second)

    made = []
    real_client = httpx2.Client

    class Spy:
        def __init__(self, **kwargs):
            made.append(kwargs)

    push._CLIENT["apns"] = None
    httpx2.Client = Spy
    try:
        shared = push._apns_client()
        again = push._apns_client()
    finally:
        httpx2.Client = real_client
    check("one client for every send, speaking HTTP/2", len(made) == 1 and shared is again
          and made[0].get("http2") is True, made)
    check("with a timeout on everything", made and made[0].get("timeout") is not None)
    push._PROVIDER.update({"token": "", "at": 0.0, "for": ()})

    section("THE IPHONE APP: WHAT IS SENT TO APPLE")
    alice, bob = person("alice@example.com", "Alice Liddell"), person("bob@example.com")
    token = "ab" * 32
    conn = db.connect(DB)
    apple = use_apple(lambda r: (200, ""))
    phone = push.register(conn, alice.id, "apns", token, label="iPhone app")
    hit = alerts.Fired(41, "AAA", "price_above", 90.0, 100.0, "AAA is 100.00, above "
                       "the 90.00 you set.", "", alice.id, "11 Nov 21:00 UTC")
    out = push.deliver(conn, [hit], SITE)
    check("one notice, sent", out["sent"] == 1 and len(apple.requests) == 1, out)
    sent = apple.requests[0]
    check("to production first, at /3/device/<token>",
          str(sent.url) == "https://api.push.apple.com/3/device/" + token, sent.url)
    check("authorised with the token", sent.headers["authorization"].startswith("bearer ")
          and verifies(sent.headers["authorization"][7:], apns_key.public_key()))
    check("for the app's bundle id", sent.headers["apns-topic"] == "app.orenth.ios")
    check("as an alert, at once", sent.headers["apns-push-type"] == "alert"
          and sent.headers["apns-priority"] == "10")
    check("given up after a while", int(sent.headers["apns-expiration"]) > time.time())
    check("replacing the same alert's last notice",
          sent.headers["apns-collapse-id"] == "alert-41")
    payload = json.loads(sent.content)
    check("saying what fired, with when the price was set",
          payload["aps"]["alert"] == {"title": "Orenth", "body": "AAA is 100.00, above the "
                                      "90.00 you set. (price as of 11 Nov 21:00 UTC)"},
          payload)
    check("with a sound and a thread", payload["aps"]["sound"] == "default"
          and payload["aps"]["thread-id"])
    check("and the page to open, beside aps", payload["url"] == "/monitor#told")
    row = device_rows(alice.id)[0]
    check("the device says when it last worked", row["last_ok_ts"] and not row["last_error"])
    _os.environ["APNS_BUNDLE_ID"] = "app.orenth.beta"
    push.send_test(conn, alice.id, SITE)
    check("APNS_BUNDLE_ID changes the topic",
          apple.requests[-1].headers["apns-topic"] == "app.orenth.beta")
    _os.environ.pop("APNS_BUNDLE_ID")

    section("THE IPHONE APP: PRODUCTION, THEN THE SANDBOX")
    conn.execute("DELETE FROM push_devices")
    conn.commit()
    debug = "cd" * 32
    push.register(conn, alice.id, "apns", debug)
    apple = use_apple(lambda r: (400, "BadDeviceToken") if "sandbox" not in str(r.url)
                      else (200, ""))
    out = push.send_test(conn, alice.id, SITE)
    hosts = [r.url.host for r in apple.requests]
    check("a token production does not know is tried in the sandbox",
          out["sent"] == 1 and hosts == ["api.push.apple.com", "api.sandbox.push.apple.com"],
          hosts)
    check("and the sandbox is remembered", device_rows(alice.id)[0]["environment"] == "sandbox")
    apple.requests.clear()
    push.send_test(conn, alice.id, SITE)
    check("so the next notice goes straight there",
          [r.url.host for r in apple.requests] == ["api.sandbox.push.apple.com"])

    section("DEVICES THAT ARE GONE ARE FORGOTTEN")
    apple = use_apple(lambda r: (410, "Unregistered"))
    out = push.send_test(conn, alice.id, SITE)
    check("Apple's Unregistered forgets the iPhone", out["dropped"] == 1
          and not device_rows(alice.id))
    push.register(conn, alice.id, "apns", debug)
    apple = use_apple(lambda r: (400, "BadDeviceToken"))
    push.send_test(conn, alice.id, SITE)
    check("so does a token neither environment knows", not device_rows(alice.id)
          and len(apple.requests) == 2)

    ua_key, ua_auth, keys_part = browser()
    endpoint, p256dh, auth = push.clean_subscription(subscription(FCM + "gone", keys_part))
    push.register(conn, alice.id, "web", endpoint, p256dh, auth)
    out = push.send_test(conn, alice.id, SITE, web_post=Recorder(410))
    check("a browser whose push service says 410 is forgotten",
          out["dropped"] == 1 and not device_rows(alice.id))
    push.register(conn, alice.id, "web", endpoint, p256dh, auth)
    push.send_test(conn, alice.id, SITE, web_post=Recorder(404))
    check("and 404", not device_rows(alice.id))

    push.register(conn, alice.id, "web", endpoint, p256dh, auth)
    for _ in range(push.MAX_FAILURES - 1):
        push.send_test(conn, alice.id, SITE, web_post=Recorder(400))
    row = device_rows(alice.id)[0]
    check("one that keeps being refused is kept a while, with the reason in words",
          row["failures"] == push.MAX_FAILURES - 1
          and row["last_error"] == "the notification service turned it away", dict(row))
    check("and the service's code in the server log only", "400" not in row["last_error"]
          and any(line.endswith("turned it away (400)") for line in LOGGED.lines),
          LOGGED.lines[-2:])
    push.send_test(conn, alice.id, SITE, web_post=Recorder(201))
    check("one that works again starts afresh", device_rows(alice.id)[0]["failures"] == 0)
    for _ in range(push.MAX_FAILURES):
        push.send_test(conn, alice.id, SITE, web_post=Recorder(400))
    check("and is forgotten once it has failed %d times in a row" % push.MAX_FAILURES,
          not device_rows(alice.id))
    push.register(conn, alice.id, "web", endpoint, p256dh, auth)
    for _ in range(push.MAX_FAILURES + 2):
        push.send_test(conn, alice.id, SITE, web_post=Recorder(503))
    row = device_rows(alice.id)
    check("a push service that is down is not held against the device",
          row and row[0]["failures"] == 0
          and row[0]["last_error"] == "the notification service had a fault just now"
          and any(line.endswith("(503)") for line in LOGGED.lines))
    conn.execute("DELETE FROM push_devices")
    conn.execute("INSERT INTO push_devices (user_id, kind, address, p256dh, auth, "
                 "created_ts) VALUES (?, 'web', 'https://evil.example/x', ?, ?, 1)",
                 (alice.id, p256dh, auth))
    conn.execute("INSERT INTO push_devices (user_id, kind, address, created_ts) "
                 "VALUES (?, 'apns', '../../3/device/other', 1)", (alice.id,))
    conn.commit()
    post, apple = Recorder(201), use_apple(lambda r: (200, ""))
    push.send_test(conn, alice.id, SITE, web_post=post)
    check("a stored address that is not a push service, or a token that is not "
          "one, is never sent to, and is forgotten",
          not post.calls and not apple.requests and not device_rows(alice.id))
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    section("APPLE REFUSING THE SERVER, OR ASKING IT TO SLOW DOWN")
    push.register(conn, alice.id, "apns", "11" * 32)
    push.register(conn, alice.id, "apns", "22" * 32)
    push._PROVIDER.update({"token": "", "at": 0.0, "for": ()})
    push.provider_token()
    push._PROVIDER["at"] -= 25 * 60          # made 25 minutes ago: renewable
    apple = use_apple(lambda r: (403, "InvalidProviderToken"))
    out = push.send_test(conn, alice.id, SITE)
    bearers = [r.headers["authorization"] for r in apple.requests]
    check("a token Apple refuses is made again once, and tried once more",
          len(bearers) == 2 and bearers[0] != bearers[1], len(bearers))
    check("then reported, and the other iPhone not tried this round",
          out["failed"] == 2 and len(apple.requests) == 2, out)
    rows = device_rows(alice.id)
    check("without holding the server's fault against the devices",
          len(rows) == 2 and all(r["failures"] == 0 for r in rows)
          and "not this iPhone's" in (rows[0]["last_error"] or "")
          and "InvalidProviderToken" not in (rows[0]["last_error"] or ""))
    check("whose code Apple gave goes to the server log for whoever runs it",
          any(line.endswith("(403 InvalidProviderToken)") for line in LOGGED.lines),
          LOGGED.lines[-2:])
    apple = use_apple(lambda r: (429, "TooManyProviderTokenUpdates"))
    push.send_test(conn, alice.id, SITE)
    apple.requests.clear()
    out = push.send_test(conn, alice.id, SITE)
    check("asked to slow down, the next round waits rather than asking again",
          not apple.requests and out["failed"] == 2)
    push._APNS_PAUSED["until"] = 0.0
    # TooManyRequests is Apple's answer for one token sent too much.
    apple = use_apple(lambda r: (429, "TooManyRequests") if str(r.url).endswith("11" * 32)
                      else (200, ""))
    out = push.send_test(conn, alice.id, SITE)
    check("one iPhone sent too much does not hold back the other",
          len(apple.requests) == 2 and out["sent"] == 1 and out["failed"] == 1, out)
    apple.requests.clear()
    out = push.send_test(conn, alice.id, SITE)
    check("nor pause the next round", len(apple.requests) == 2 and out["sent"] == 1, out)
    check("and is not held against that iPhone",
          all(r["failures"] == 0 for r in device_rows(alice.id)))
    push._APNS_PAUSED["until"] = 0.0

    def timeout(request):
        raise httpx2.ConnectTimeout("no answer")

    apple = use_apple(timeout)
    out = push.send_test(conn, alice.id, SITE)
    check("Apple not answering is said once, not waited on for each iPhone",
          len(apple.requests) == 1 and out["failed"] == 2, len(apple.requests))
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    # A typo in APNS_BUNDLE_ID, or a changed one: Apple says every token is
    # not for that topic, and the fault is the server's.
    _os.environ["APNS_BUNDLE_ID"] = "app.orenth.iso"
    phones = []
    for n in range(3):
        someone = person("topic-%d@example.com" % n)
        push.register(conn, someone.id, "apns", ("3%d" % n) * 32)
        phones.append(alerts.Fired(80 + n, "AAA", "price_above", 1.0, 2.0, "AAA moved.", "",
                                   someone.id, ""))
    conn.execute("UPDATE push_devices SET last_ok_ts=?", (int(time.time()) - 3600,))
    conn.commit()
    apple = use_apple(lambda r: (400, "DeviceTokenNotForTopic"))
    out = push.deliver(conn, phones, SITE)
    kept = conn.execute("SELECT last_error FROM push_devices WHERE kind='apns'").fetchall()
    check("iPhones that have had notices are kept when Apple calls the topic wrong",
          out["dropped"] == 0 and len(kept) == 3, out)
    check("Apple is asked once that round, not about each",
          len(apple.requests) == 1, len(apple.requests))
    # Plain words for the person; the bundle id it named goes to the log,
    # for whoever runs the server to correct.
    check("and the device says so in plain words",
          kept and "names the app differently" in (kept[0]["last_error"] or "")
          and "app.orenth.iso" not in (kept[0]["last_error"] or ""),
          [r["last_error"] for r in kept])
    check("while the server log names the app id it used",
          any("app.orenth.iso" in line for line in LOGGED.lines), LOGGED.lines[-3:])
    _os.environ.pop("APNS_BUNDLE_ID")
    conn.execute("DELETE FROM push_devices")
    conn.commit()
    pasted = person("topic-new@example.com")
    push.register(conn, pasted.id, "apns", "40" * 32)          # never had a notice
    push.register(conn, pasted.id, "apns", "41" * 32)
    conn.execute("UPDATE push_devices SET last_ok_ts=? WHERE address=?",
                 (int(time.time()) - 3600, "41" * 32))
    conn.commit()
    apple = use_apple(lambda r: (400, "DeviceTokenNotForTopic")
                      if str(r.url).endswith("40" * 32) else (200, ""))
    out = push.send_test(conn, pasted.id, SITE)
    check("a token that never had a notice, for another app, is forgotten, and the "
          "round goes on", out["dropped"] == 1 and out["sent"] == 1
          and [d["address"] for d in device_rows(pasted.id)] == ["41" * 32], out)
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    section("ONE BROWSER'S TROUBLE IS NOT EVERYONE'S")
    use_apple(lambda r: (200, ""))
    busy = []
    for n in range(3):
        someone = person("busy-%d@example.com" % n)
        endpoint, p256dh, auth = push.clean_subscription(subscription(FCM + "busy-%d" % n))
        push.register(conn, someone.id, "web", endpoint, p256dh, auth)
        busy.append(alerts.Fired(70 + n, "AAA", "price_above", 1.0, 2.0, "AAA moved.", "",
                                 someone.id, ""))
    for answer, why in ((503, "fails quickly for one browser"),
                        (429, "asks Orenth to slow down for one browser")):
        post = Recorder(lambda url, answer=answer: answer if url.endswith("busy-0") else 201)
        out = push.deliver(conn, busy, SITE, web_post=post)
        check("a push service that %s still gets the others' notices" % why,
              len(post.calls) == 3 and out["sent"] == 2 and out["failed"] == 1, out)
    post = Recorder(lambda url: TimeoutError("no answer") if url.endswith("busy-0") else 201)
    out = push.deliver(conn, busy, SITE, web_post=post)
    check("one that does not answer at all is not waited on again this round",
          len(post.calls) == 1 and out["failed"] == 3, out)
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    section("A PUSH SERVICE THAT NEVER ANSWERS")
    stalled = person("stalled@example.com")
    for n in range(10):
        endpoint, p256dh, auth = push.clean_subscription(
            subscription("https://h%d.notify.windows.com/w/?token=t%d" % (n, n)))
        push.register(conn, stalled.id, "web", endpoint, p256dh, auth)
    post = Recorder(TimeoutError("no answer"))
    push.deliver(conn, [alerts.Fired(90, "AAA", "price_above", 1.0, 2.0, "AAA moved.", "",
                                     stalled.id, "")], SITE, web_post=post)
    check("ten addresses on ten hosts of one push service are waited on once a round",
          len(post.calls) == 1, len(post.calls))
    post = Recorder(TimeoutError("no answer"))
    push.send_test(conn, stalled.id, SITE, web_post=post)
    check("and once a test", len(post.calls) == 1, len(post.calls))
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    month_ago = int(time.time()) - 31 * 86400
    endpoint, p256dh, auth = push.clean_subscription(subscription(FCM + "stalled"))
    push.register(conn, stalled.id, "web", endpoint, p256dh, auth)
    conn.execute("UPDATE push_devices SET created_ts=? WHERE address=?", (month_ago, endpoint))
    conn.commit()
    for _ in range(push.MAX_FAILURES):
        push.send_test(conn, stalled.id, SITE, web_post=Recorder(TimeoutError("no answer")))
    check("an address no notice has reached in a month, still not answering, is forgotten",
          not device_rows(stalled.id))
    push.register(conn, stalled.id, "web", endpoint, p256dh, auth)
    conn.execute("UPDATE push_devices SET created_ts=?, last_ok_ts=? WHERE address=?",
                 (month_ago, int(time.time()) - 86400, endpoint))
    conn.commit()
    for _ in range(push.MAX_FAILURES + 2):
        push.send_test(conn, stalled.id, SITE, web_post=Recorder(TimeoutError("no answer")))
    rows = device_rows(stalled.id)
    check("one a notice reached yesterday is kept through an outage",
          rows and rows[0]["failures"] == 0, [dict(r) for r in rows])
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    budget = getattr(push, "TEST_BUDGET", 20.0)
    push.TEST_BUDGET = 0.5
    try:
        for url in (FCM + "slow", "https://android.googleapis.com/gcm/send/slow",
                    "https://updates.push.services.mozilla.com/wpush/v2/slow",
                    "https://web.push.apple.com/slow"):
            endpoint, p256dh, auth = push.clean_subscription(subscription(url))
            push.register(conn, stalled.id, "web", endpoint, p256dh, auth)
        post = Recorder(201, delay=0.3)
        out = push.send_test(conn, stalled.id, SITE, web_post=post)
        check("a test stops starting on more devices once its time is up",
              len(post.calls) == 2 and out["sent"] == 2 and out["failed"] == 2, out)
    finally:
        push.TEST_BUDGET = budget
    conn.execute("DELETE FROM push_devices")
    conn.commit()

    section("A DEVICE BELONGS TO ONE PERSON")
    ua_key, ua_auth, keys_part = browser()
    endpoint, p256dh, auth = push.clean_subscription(subscription(FCM + "shared", keys_part))
    first_id = push.register(conn, alice.id, "web", endpoint, p256dh, auth, label="Alice's")
    push.register(conn, bob.id, "web", endpoint, p256dh, auth, label="Bob's")
    rows = conn.execute("SELECT user_id, label FROM push_devices WHERE address=?",
                        (endpoint,)).fetchall()
    check("switched on by someone else, it moves to them", len(rows) == 1
          and rows[0]["user_id"] == bob.id, [dict(r) for r in rows])
    post = Recorder(201)
    push.deliver(conn, [alerts.Fired(1, "AAA", "price_above", 1.0, 2.0, "Alice's level",
                                     "", alice.id, "")], SITE, web_post=post)
    check("so the shared phone is not sent the last owner's alerts", not post.calls)
    push.deliver(conn, [alerts.Fired(2, "BBB", "price_above", 1.0, 2.0, "Bob's level",
                                     "", bob.id, "")], SITE, web_post=post)
    check("but is sent the new owner's", len(post.calls) == 1
          and json.loads(decrypt(post.calls[0][1], ua_key, ua_auth))["body"].startswith(
              "Bob's level"))

    carol = person("carol@example.com")
    for n in range(push.MAX_DEVICES + 2):
        push.register(conn, carol.id, "apns", "%064x" % (n + 1))
        conn.execute("UPDATE push_devices SET created_ts=? WHERE address=?",
                     (1000 + n, "%064x" % (n + 1)))
        conn.commit()
    rows = device_rows(carol.id)
    kept = {r["address"] for r in rows}
    check("at most %d devices a person" % push.MAX_DEVICES, len(rows) == push.MAX_DEVICES)
    check("the newest kept, the ones quiet longest let go",
          "%064x" % (push.MAX_DEVICES + 2) in kept and "%064x" % 1 not in kept
          and "%064x" % 2 not in kept)

    erin = person("erin@example.com")
    push.register(conn, erin.id, "apns", "ee" * 32)
    removed = A.delete_user(conn, erin.id)
    check("deleting an account deletes its devices",
          removed.get("push_devices") == 1 and not device_rows(erin.id), removed)
    frank = person("frank@example.com")
    push.register(conn, frank.id, "apns", "ff" * 32)
    conn.execute("DELETE FROM sessions WHERE user_id=?", (frank.id,))
    conn.execute("DELETE FROM users WHERE id=?", (frank.id,))
    conn.commit()
    check("and an older version that knows nothing of them can still delete one",
          not conn.execute("SELECT COUNT(*) FROM push_devices WHERE user_id=?",
                           (frank.id,)).fetchone()[0])
    conn.close()

    section("WHAT THE ENDPOINTS REFUSE")
    conn = db.connect(DB)
    conn.execute("DELETE FROM push_devices")
    conn.commit()
    conn.close()
    use_apple(lambda request: (200, ""))
    web._RATE.clear()
    sign_out()
    good = subscription(FCM + "page")
    check("nobody signed in: refused", c.post("/api/push/subscribe", json={
        "kind": "web", "subscription": good}).status_code == 401)
    check("nor told the key", c.get("/api/push/config").status_code == 401)
    sign_in(alice)
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": good},
               headers={"Origin": "https://evil.example"})
    check("a post from another site's page is refused", r.status_code == 403, r.status_code)
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": good},
               headers={"Origin": "null"})
    check("so is one from nowhere in particular", r.status_code == 403)
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": good},
               headers={"Sec-Fetch-Site": "cross-site"})
    check("and one the browser says crossed sites", r.status_code == 403)
    r = c.post("/api/push/subscribe", data={"kind": "web"})
    check("a form post, which another site could send, is refused", r.status_code == 400)
    r = c.post("/api/push/subscribe", data=json.dumps({"kind": "web", "x": "y" * 20000}),
               content_type="application/json")
    check("as is a body far bigger than a device's details", r.status_code == 413)
    # Under that size, JSON nested far deeper than Python can read.
    deep = "[" * 8000 + "]" * 8000
    for path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"):
        try:
            status = c.post(path, data=deep, content_type="application/json").status_code
        except RecursionError:              # TESTING hands the server's error on
            status = 500
        check("JSON nested thousands deep, sent to %s, is refused as not JSON" % path,
              status == 400, status)
    for name, sub in (("plain http", subscription("http://fcm.googleapis.com/fcm/send/x")),
                      ("another host", subscription("https://evil.example/push")),
                      ("a host hidden in the user name",
                       subscription("https://fcm.googleapis.com@evil.example/x")),
                      ("a key that is not a point", dict(good, keys={
                          "p256dh": push.b64url(b"\x04" + b"\x01" * 64),
                          "auth": good["keys"]["auth"]})),
                      ("a secret of the wrong size", dict(good, keys={
                          "p256dh": good["keys"]["p256dh"], "auth": "AAAA"}))):
        r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": sub})
        check("a subscription with %s is refused" % name, r.status_code == 400,
              (r.status_code, r.get_json()))
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": good},
               headers={"Origin": "http://localhost"})
    check("from this site's own page it is taken", r.status_code == 200
          and r.get_json()["ok"], r.get_json())
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": good},
               headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"})
    rows = device_rows(alice.id)
    check("again, it is the same device, named from its browser", len(rows) == 1
          and rows[0]["label"] == "Chrome on Android", [dict(x) for x in rows])
    r = c.post("/api/push/subscribe", json={"kind": "apns", "token": "zz"})
    check("a token that is not hex is refused", r.status_code == 400)
    _os.environ.pop("APNS_KEY_ID")
    r = c.post("/api/push/subscribe", json={"kind": "apns", "token": "ab" * 32})
    check("an iPhone token while the app's notifications are off is refused",
          r.status_code == 409)
    _os.environ["APNS_KEY_ID"] = "KEY1234567"
    r = c.post("/api/push/subscribe", json={"kind": "apns", "token": ("AB" * 32),
                                            "environment": "sandbox"})
    check("with them on it is taken, in lower case, where the app says it lives",
          r.status_code == 200 and any(d["address"] == "ab" * 32 and
                                       d["environment"] == "sandbox"
                                       for d in device_rows(alice.id)))
    for odd in (["x"], {"a": 1}, 5):
        try:
            r = c.post("/api/push/subscribe", json={"kind": "apns", "token": "cd" * 32,
                                                    "environment": odd})
            answered = r.status_code == 200 and r.is_json and r.get_json()["ok"]
        except Exception as exc:            # TESTING hands the server's error on
            answered = exc
        check("an environment that is not a name (%r) is left out, not an error" % (odd,),
              answered is True and any(d["address"] == "cd" * 32 and d["environment"] == ""
                                       for d in device_rows(alice.id)), answered)
    c.post("/api/push/unsubscribe", json={"kind": "apns", "token": "cd" * 32})
    check("named the iPhone app when its browser says so", push.device_label(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 OrenthApp/1") == "iPhone app")

    sign_in(bob)
    r = c.post("/api/push/unsubscribe", json={"kind": "web", "endpoint": good["endpoint"]})
    check("nobody can switch off someone else's device",
          r.get_json()["removed"] == 0 and len(device_rows(alice.id)) == 2)
    r = c.post("/api/push/test", json={})
    check("a test for someone with no device says so", r.status_code == 404)
    conn = db.connect(DB)
    alice_ids = [d["id"] for d in push.devices(conn, alice.id)]
    conn.close()
    c.post("/settings/notifications/remove", data={"device_id": alice_ids[0]})
    check("not from the Settings page either", len(device_rows(alice.id)) == 2)
    sign_in(alice)
    r = c.post("/api/push/unsubscribe", json={"kind": "apns", "token": "AB" * 32})
    check("the owner can switch one off", r.get_json()["removed"] == 1
          and len(device_rows(alice.id)) == 1)
    page = c.get("/settings").data.decode("utf-8", "replace")
    check("the Settings page lists the devices", "Chrome on Android" in page
          and 'action="/settings/notifications/remove"' in page)
    r = c.post("/settings/notifications/remove", data={"device_id": alice_ids[0]})
    check("and removes one from there", r.status_code == 302 and not device_rows(alice.id))

    push._web_post = Recorder(201)
    c.post("/api/push/subscribe", json={"kind": "web", "subscription": good})
    web._RATE.clear()
    r = c.post("/api/push/test", json={"kind": "web", "endpoint": good["endpoint"]})
    check("a test reaches the device", r.status_code == 200 and r.get_json()["sent"] == 1
          and len(push._web_post.calls) == 1, r.get_json())
    for _ in range(web.PUSH_TESTS_PER_HOUR):
        r = c.post("/api/push/test", json={})
    check("and tests are limited", r.status_code == 429, r.status_code)
    web._RATE.clear()
    sign_in(bob)
    r = c.post("/api/push/unsubscribe", json={"kind": "web", "endpoint": good["endpoint"],
                                              "release": True})
    check("but the browser it is on can let it go, whoever is signed in there now",
          r.get_json()["removed"] == 1 and not device_rows(alice.id))

    section("A CHECK KEEPS A DEVICE UP TO DATE, AND NEVER BRINGS ONE BACK")
    henry = person("henry@example.com")
    sign_in(henry)
    sub = subscription(FCM + "henry")
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": sub, "check": True})
    check("a check for a device nobody turned on keeps nothing",
          r.get_json() == {"ok": True, "known": False} and not device_rows(henry.id))
    c.post("/api/push/subscribe", json={"kind": "web", "subscription": sub})
    first_ref = device_rows(henry.id)[0]["session_ref"]
    sign_in(henry)                      # signed in again, in the same browser
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": sub, "check": True})
    rows = device_rows(henry.id)
    check("a check for one that is on keeps it, under the sign-in of now",
          r.get_json()["known"] is True and len(rows) == 1
          and rows[0]["session_ref"] not in ("", first_ref))
    renewed = subscription(FCM + "henry-renewed")
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": renewed,
                                            "check": True, "replaces": sub["endpoint"]})
    check("a renewed subscription takes the old one's place",
          r.get_json()["known"] is True
          and [d["address"] for d in device_rows(henry.id)] == [renewed["endpoint"]])
    c.post("/settings/notifications/remove",
           data={"device_id": device_rows(henry.id)[0]["id"]})
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": renewed,
                                            "check": True})
    check("one removed from another device stays removed",
          r.get_json()["known"] is False and not device_rows(henry.id))
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": subscription(
        FCM + "henry-new"), "check": True, "replaces": FCM + "never-his"})
    check("and a renewal in place of a device that was never theirs keeps nothing",
          r.get_json()["known"] is False and not device_rows(henry.id))
    web._RATE.clear()

    section("SIGNING OUT FORGETS WHAT THAT SIGN-IN SWITCHED ON")
    dave = person("dave@example.com")
    sign_in(dave)
    phone_sub = subscription(FCM + "daves-phone")
    c.post("/api/push/subscribe", json={"kind": "web", "subscription": phone_sub})
    laptop = web.app.test_client()
    sign_in(dave, laptop)
    laptop.post("/api/push/subscribe", json={"kind": "web",
                                             "subscription": subscription(FCM + "laptop")})
    check("two devices, from two sign-ins", len(device_rows(dave.id)) == 2)
    c.post("/signout")
    left = [r["address"] for r in device_rows(dave.id)]
    check("signing out on the phone forgets the phone", left == [FCM + "laptop"], left)
    sign_in(dave)
    r = c.post("/api/push/subscribe", json={"kind": "web", "subscription": phone_sub,
                                            "check": True})
    check("and signing in there again does not bring it back",
          r.get_json()["known"] is False
          and [d["address"] for d in device_rows(dave.id)] == [FCM + "laptop"])

    section("A SIGN-IN OVER ANOTHER ENDS IT, AND TAKES THEIR DEVICE OFF")
    ann = person("ann-shared@example.com")
    ben = person("ben-shared@example.com")

    def signs_in(user):
        """A real sign-in, in the browser c is."""
        with c.session_transaction() as sess:
            held = dict(sess)
        with web.app.test_request_context("/"):
            web.session.update(held)
            web._open_session(user)
            now = dict(web.session)
        with c.session_transaction() as sess:
            sess.clear()
            sess.update(now)
        return held.get(web.SESSION_KEY)

    def alive(token):
        conn = db.connect(DB)
        try:
            return A.user_for_session(conn, token) is not None
        finally:
            conn.close()

    sign_out()
    signs_in(ann)
    c.post("/api/push/subscribe", json={"kind": "web",
                                        "subscription": subscription(FCM + "shared")})
    first = signs_in(ann)
    check("the same person signing in again ends the first sign-in", not alive(first))
    check("and keeps their own device on", len(device_rows(ann.id)) == 1)
    c.post("/api/push/subscribe", json={"kind": "web", "check": True,
                                        "subscription": subscription(FCM + "shared")})
    second = signs_in(ben)
    check("someone else signing in there ends it too", not alive(second))
    check("and takes the device off the person before", not device_rows(ann.id))
    check("while they are signed in themselves",
          "ben-shared@example.com" in c.get("/account").data.decode("utf-8", "replace"))
    web._RATE.clear()

    section("FROM A FIRED ALERT TO A NOTIFICATION")
    web.market_mod.split_history = lambda symbol: []
    web._call_now = lambda symbol, interval="1d": None

    def bars(symbol, interval="1d", lookback=None):
        return SimpleNamespace(symbol=symbol, last_price=100.0, close=[100.0] * 60,
                               volume=[1] * 60, prev_close=100.0, market_open=False,
                               last_bar_time="2026-11-13 16:00",
                               meta={"regularMarketTime": 1763067600})

    alerts.fetch_bars = bars
    grace = person("grace@example.com")
    ua_key, ua_auth, keys_part = browser()
    conn = db.connect(DB)
    endpoint, p256dh, auth = push.clean_subscription(subscription(FCM + "grace", keys_part))
    push.register(conn, grace.id, "web", endpoint, p256dh, auth)
    level = db.alert_add(conn, "AAA", "price_above", 90.0, user=grace.id)
    push._web_post = Recorder(201)
    result = web._check_and_tell(conn, user=grace.id)
    check("the alert fires", [f.alert_id for f in result["fired"]] == [level])
    check("and the notice is sent", push.wait_idle(10) and len(push._web_post.calls) == 1,
          len(push._web_post.calls))
    url, body, headers = push._web_post.calls[0]
    check("to the browser's push service", url == endpoint)
    check("encrypted (aes128gcm)", headers["Content-Encoding"] == "aes128gcm"
          and headers["Content-Type"] == "application/octet-stream")
    check("signed with the server's key", headers["Authorization"].startswith("vapid t=")
          and push.vapid_keys().public in headers["Authorization"])
    check("urgent, kept for hours, and replacing the same alert's last one",
          headers["Urgency"] == "high" and headers["TTL"] == str(push.ALERT_TTL)
          and headers["Topic"] == "alert-%d" % level, headers)
    note = json.loads(decrypt(body, ua_key, ua_auth))
    message = result["fired"][0].message
    check("readable only by that browser, saying what fired and when the price was set",
          note == {"title": "Orenth", "tag": "alert-%d" % level,
                   "url": "/monitor?told=%d#told" % result["fired"][0].event_id,
                   "body": "%s (price as of %s)" % (message, result["fired"][0].as_of)},
          note)
    check("in the alert email's own words", note["body"].startswith(message)
          and "(price as of " in note["body"])

    slow = Recorder(201, delay=2.0)
    push._web_post = slow
    db.alert_reset(conn, level, user=grace.id)
    started = time.time()
    web._check_and_tell(conn, user=grace.id)
    waited = time.time() - started
    check("a slow push service never holds up the check", waited < 1.5, "%.2fs" % waited)
    check("which still sends, on its own thread", push.wait_idle(10) and len(slow.calls) == 1)

    real_deliver = push.deliver

    def broken(*args, **kwargs):
        raise RuntimeError("the push sender broke")

    push.deliver = broken
    db.alert_reset(conn, level, user=grace.id)
    try:
        result = web._check_and_tell(conn, user=grace.id)
        check("a failing sender never fails the check", len(result["fired"]) == 1)
        push.wait_idle(10)
    except Exception as exc:
        check("a failing sender never fails the check", False, exc)
    finally:
        push.deliver = real_deliver
    push._web_post = Recorder(201)
    db.alert_reset(conn, level, user=grace.id)
    web._check_and_tell(conn, user=grace.id)
    check("and the next round is sent as usual",
          push.wait_idle(10) and len(push._web_post.calls) == 1)

    nobody = person("nobody@example.com")
    quiet = db.alert_add(conn, "AAA", "price_above", 90.0, user=nobody.id)
    push._web_post = Recorder(201)
    web._check_and_tell(conn, user=nobody.id)
    push.wait_idle(10)
    sign_in(grace)
    check("the page it opens has the list it names",
          'id="told"' in c.get(note["url"].split("#")[0]).data.decode("utf-8"))
    check("nothing is sent for someone with no device on", not push._web_post.calls)
    db.alert_delete(conn, quiet, user=nobody.id)

    _os.environ["VAPID_PRIVATE_KEY"] = "not a key"
    db.alert_reset(conn, level, user=grace.id)
    web._check_and_tell(conn, user=grace.id)
    push.wait_idle(10)
    row = device_rows(grace.id)[0]
    check("with browser notifications off, nothing is sent and the device is kept",
          not push._web_post.calls and row["failures"] == 0 and row["last_error"])
    _os.environ.pop("VAPID_PRIVATE_KEY")

    many = [alerts.Fired(i, "S%d" % i, "price_above", 1.0, 2.0, "S%d moved." % i, "",
                         grace.id, "13 Nov") for i in range(1, 7)]
    told = push.alert_note(many)
    check("several at once are one notice, counted like the email's subject",
          told.title == "Orenth: 6 alerts" and told.body.count("(price as of 13 Nov)") == 4
          and told.body.endswith("And 2 more on your Monitor page."), told.body)
    stop = alerts.Fired(0, "CCC", "stop_breach", 21.0, 20.0, "CCC is past the stop.", "",
                        grace.id, "", trade_id=7)
    check("a crossed stop replaces its own last notice", push.alert_note([stop]).tag == "stop-7")
    check("a tag too long for the Topic header is shortened", len(told.topic) <= 32
          and re.fullmatch(r"[A-Za-z0-9_-]+", told.topic))
    conn.close()

    section("THE INSTALLABLE APP")
    sign_out()
    r = c.get("/manifest.webmanifest")
    check("the manifest is public", r.status_code == 200, r.status_code)
    check("and served as a manifest", r.mimetype == "application/manifest+json", r.mimetype)
    m = json.loads(r.data)
    check("it names the app and where it opens",
          (m["id"], m["name"], m["short_name"], m["start_url"], m["scope"], m["display"],
           m["orientation"]) == ("/", "Orenth", "Orenth", "/?source=app", "/", "standalone",
                                 "any"), m)
    css = open(_os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    check("in the site's own colours", m["background_color"] in css
          and m["theme_color"] in css and m["background_color"] == "#191817")
    check("as a finance app", m["categories"] == ["finance"])
    purposes = {(i["purpose"], i["sizes"]) for i in m["icons"]}
    check("with icons for any shape and maskable ones, at 192 and 512",
          {("any", "192x192"), ("any", "512x512"), ("maskable", "192x192"),
           ("maskable", "512x512")} <= purposes, purposes)
    for icon in m["icons"]:
        got = c.get(icon["src"])
        size = Image.open(_os.path.join(ROOT, icon["src"].lstrip("/"))).size
        check("%s is there at %s" % (icon["src"], icon["sizes"]), got.status_code == 200
              and "%dx%d" % size == icon["sizes"])
    check("with Analyse, Monitor and Journal on a long press",
          [(s["name"], s["url"]) for s in m["shortcuts"]]
          == [("Analyse", "/"), ("Monitor", "/monitor"), ("Journal", "/trades")])

    r = c.get("/sw.js")
    source = r.data.decode("utf-8")
    check("the service worker is served from the root", r.status_code == 200)
    check("as JavaScript", r.mimetype == "application/javascript", r.mimetype)
    check("checked for a new version on every visit",
          r.headers.get("Cache-Control") == "no-cache")
    check("allowed to cover the whole site", r.headers.get("Service-Worker-Allowed") == "/")
    check("its cache named for this release's files",
          "'orenth-' + VERSION" in source and json.dumps(web.ASSET_VERSION) in source)
    sign_in(alice)
    check("and the same file whoever asks", c.get("/sw.js").data.decode("utf-8") == source)
    sign_out()
    kept_list = json.loads(re.search(r"const PRECACHE = (\[.*?\]);", source).group(1))
    check("it keeps only public files under /static/",
          kept_list and all(u.startswith("/static/") for u in kept_list), kept_list)
    marked = "?v=" + web.ASSET_VERSION
    check("including the style sheet and the icons",
          {"/static/style.css" + marked, "/static/icon-192.png" + marked,
           "/static/badge-96.png" + marked} <= set(kept_list), kept_list)
    # A version from before the worker asks for /static/style.css plain, so
    # after a rollback it can never be handed this release's copy.
    check("each one's address marked with this release's version, as the cache's name is",
          all(u.endswith(marked) for u in kept_list), kept_list)
    check("and so are the notification's icon and badge",
          "const ICON = %s;" % json.dumps("/static/icon-192.png" + marked) in source
          and "const BADGE = %s;" % json.dumps("/static/badge-96.png" + marked) in source)
    page_fn = source[source.index("async function page("):source.index("async function file(")]
    file_fn = source[source.index("async function file("):source.index("function keepable(")]
    check("a page always comes from the network and is never kept",
          "fetch(event.request)" in page_fn and "put(" not in page_fn
          and "add(" not in page_fn)
    check("only a page that cannot be fetched gets the offline one",
          "catch" in page_fn and "cache.match(OFFLINE)" in page_fn)
    check("the one place anything is kept is the static-file handler",
          source.count("cache.put(") == 1 and "cache.put(" in file_fn
          and "keepable(fresh)" in file_fn)
    keepable = source[source.index("function keepable("):source.index("function samePath(")]
    check("and it refuses anything that answers as a page or as data, whatever its address",
          "'html'" in keepable and "'json'" in keepable)
    check("only GET from this site is handled; a form and /api go to the network",
          "request.method !== 'GET'" in source and "url.origin !== self.location.origin"
          in source and "url.pathname.startsWith(STATIC)" in source)
    check("old caches are deleted when a new version takes over",
          "caches.delete" in source and "name !== CACHE" in source)
    check("it shows a pushed notification with the icon, the badge, a tag and the page",
          all(x in source for x in ("showNotification", "icon: ICON", "badge: BADGE",
                                    "tag: tag", "data: { url: samePath(note.url) }")))
    check("a tap opens Orenth, only ever on a path of its own",
          "notificationclick" in source and "openWindow" in source
          and "raw.charAt(1) === '/'" in source and "indexOf('\\\\')" in source)
    renewal = source[source.index("'pushsubscriptionchange'"):]
    check("and a replaced subscription is renewed, the server told only as a check",
          "SUBSCRIBE" in renewal and "check: true" in renewal and "replaces:" in renewal)
    check("the files it keeps are fetched without cookies", "credentials: 'omit'" in source)
    fd, worker_js = tempfile.mkstemp(suffix=".js", prefix="orenth-sw-")
    with _os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(source)
    try:
        ran = run_node(WORKER_SCOPE, worker_js, "worker", web.ASSET_VERSION)
    finally:
        _os.remove(worker_js)
    if ran is None:
        print("   (Node is not installed, so the worker is read here rather than run)")
        routed = [line for line in source.splitlines() if "url.pathname.startsWith(STATIC)" in line]
        check("it answers from its copy only for this release's marked files",
              routed and all("VERSION" in line for line in routed), routed)
    elif "error" in ran:
        check("the worker runs as a browser runs it", False, ran["error"])
    else:
        check("this release's style sheet comes from the copy kept at install",
              ran["current"] == {"answered": True, "from": "kept"}
              and not ran["current_fetched"], ran)
        check("an older version's page asking for /static/style.css is left to the "
              "server, so a rollback gets its own style sheet",
              ran["older"]["answered"] is False, ran["older"])
        check("as is one marked with another version", ran["another"]["answered"] is False,
              ran["another"])
        check("pages still come from the network through it, and /api past it",
              ran["page"] == {"answered": True, "from": "network"}
              and ran["api"]["answered"] is False, ran)

    vic = person("victoria@example.com", "Victoria Secretname")
    sign_in(vic)
    r = c.get("/offline")
    off = r.data.decode("utf-8")
    check("the offline page is there for a signed-in person", r.status_code == 200)
    check("and holds nothing about them",
          "victoria" not in off.lower() and "Secretname" not in off
          and 'id="firebar"' not in off and "data-user" not in off
          and "/account" not in off, off[:200])
    sign_out()
    check("it is the same page for anybody", c.get("/offline").data.decode("utf-8") == off)
    check("it says what is wrong and offers to try again",
          "No connection" in off and "Try again" in off)
    check("and is kept out of search results", 'name="robots" content="noindex"' in off)
    check("its style sheet and icon are the kept ones, marked with the version",
          'href="/static/style.css%s"' % marked in off
          and 'href="/static/favicon.ico%s"' % marked in off)

    home = c.get("/").data.decode("utf-8")
    check("every page links the manifest", 'rel="manifest" href="/manifest.webmanifest"' in home)
    check("and colours the browser bar for each theme",
          'media="(prefers-color-scheme: light)" content="#f4f3ee"' in home
          and 'media="(prefers-color-scheme: dark)" content="#191817"' in home)
    check("draws under a notch, kept clear by the CSS", "viewport-fit=cover" in home
          and "env(safe-area-inset-top" in css)
    check("names itself on an iPhone's home screen",
          'name="apple-mobile-web-app-title" content="Orenth"' in home
          and 'name="apple-mobile-web-app-capable" content="yes"' in home
          and 'name="mobile-web-app-capable" content="yes"' in home)
    check("keeps the iPhone's status bar readable on the light theme",
          'name="apple-mobile-web-app-status-bar-style" content="default"' in home)
    check("registers the worker after load, and never inside the iPhone app",
          "'serviceWorker' in navigator && !window.Capacitor" in home
          and "navigator.serviceWorker.register('/sw.js'" in home)
    sign_in(alice)
    hers = c.get("/privacy").data.decode("utf-8")
    sign_out()
    check("every page asks for the style sheet and its scripts marked with the version",
          all('href="/static/style.css%s"' % marked in page for page in (home, hers))
          and 'src="/static/notify.js%s"' % marked in hers)
    check("a visitor's browser is not sent the notifications script, of no use to it",
          "/static/notify.js" not in home)
    in_app = c.get("/privacy", headers={"User-Agent": "Mozilla/5.0 (iPhone) OrenthApp/1"})
    check("an app page asks for the phone script marked too, and for the one that "
          "opens a tapped notification, signed in or not",
          all('src="/static/%s%s"' % (name, marked) in in_app.data.decode("utf-8")
              for name in ("native.js", "notify.js")))

    sign_in(alice)
    account = c.get("/account").data.decode("utf-8")
    check("the Account page explains adding Orenth to the home screen",
          'id="install"' in account and "beforeinstallprompt" in account
          and "Add to Home Screen" in account and "display-mode: standalone" in account)
    monitor = c.get("/monitor").data.decode("utf-8")
    check("the Monitor page has the switch for this device, hidden until it can work",
          "data-notify hidden" in monitor and 'data-notify-act="test"' in monitor)
    check("and a button a script hides really is hidden, whatever .btn says",
          re.search(r"\.btn\[hidden\]\s*\{\s*display:\s*none", css) is not None)
    sign_out()
    demo = c.get("/monitor").data.decode("utf-8")
    check("a visitor looking at the example gets no switch", "data-notify" not in demo)

    for size in (192, 512):
        img = Image.open(_os.path.join(ROOT, "static", "icon-maskable-%d.png" % size)).convert("RGB")
        px = img.load()
        ground = px[0, 0]
        corners = {px[0, 0], px[size - 1, 0], px[0, size - 1], px[size - 1, size - 1]}
        centre = (size - 1) / 2.0
        reach = max((((x - centre) ** 2 + (y - centre) ** 2) ** 0.5)
                    for x in range(size) for y in range(size)
                    if max(abs(a - b_) for a, b_ in zip(px[x, y], ground)) > 24)
        check("the %dpx maskable icon fills the square with the dark ground" % size,
              corners == {(25, 24, 23)}, corners)
        check("and keeps the mark inside the 40%% safe circle (%.0f%%)" % (100 * reach / size),
              reach <= 0.40 * size)
    badge = Image.open(_os.path.join(ROOT, "static", "badge-96.png"))
    px = badge.convert("RGBA").load()
    pixels = [px[x, y] for x in range(badge.size[0]) for y in range(badge.size[1])]
    check("the notification badge is 96 pixels, white on nothing",
          badge.size == (96, 96) and pixels[0][3] == 0
          and {p[:3] for p in pixels if p[3]} == {(255, 255, 255)})

    stamp = tempfile.mkdtemp(prefix="stockbot-assets-")
    _os.makedirs(_os.path.join(stamp, "static"))
    with open(_os.path.join(stamp, "static", "style.css"), "w") as fh:
        fh.write("a{}")
    before = webapp.asset_version(stamp)
    with open(_os.path.join(stamp, "static", "style.css"), "w") as fh:
        fh.write("a{color:red}")
    check("a changed file is a new version, so phones fetch it again",
          webapp.asset_version(stamp) != before)

    privacy = c.get("/privacy").data.decode("utf-8")
    check("the privacy page names every push service and says what each can read",
          all(w in privacy for w in ("Apple's", "Google's", "Mozilla's", "Microsoft's",
                                     "encrypted for that browser", 'id="notifications"'))
          and "which can read them" in privacy)

    section("THE IPHONE APP FORGETS ITS DEVICE ONLY ONCE THE SERVER HAS")
    # Nothing at Apple ever retires an iPhone's token, so a device the app
    # forgets before the server has let it go keeps getting alerts for good.
    notify_js = _os.path.join(ROOT, "static", "notify.js")
    phone = {"orenth-push-owner": "2", "orenth-apns-token": "ab" * 32}
    got = run_node(NOTIFY_IN_APP, notify_js)
    if got is None:
        print("   (Node is not installed, so notify.js is read here rather than run)")
        source = open(notify_js, encoding="utf-8").read()
        disable = source[source.index("function appDisable("):
                         source.index("// --- the page's switch")]
        check("a release or a switch-off that fails never forgets the device",
              ".catch(function () {}).then(forget)" not in source)
        check("Turn off forgets it here only after the server has",
              disable.index("post(UNSUBSCRIBE") < disable.index("write(TOKEN, '')"))
    elif "error" in got:
        check("notify.js runs as the app runs it", False, got["error"])
    else:
        for name, seen in sorted(got["handover"].items()):
            check("another account's phone asks the server to let it go (%s)" % name,
                  seen["asked"], seen)
            check("and when that fails, keeps the device to ask again (%s)" % name,
                  seen["kept"] == dict(phone, **{"orenth-push-owner": "1"}), seen["kept"])
            check("the next page asks again, and only then forgets it (%s)" % name,
                  seen["asked_again"] and seen["after"] == {}, seen)
        off = got["turn_off"]
        check("Turn off with no connection says it did not work, and stays On",
              off["before"] == "On" and off["failed"]["chip"] == "On"
              and off["failed"]["said"].startswith("That did not work"), off)
        check("and ends that with one full stop, whatever the reason ended with",
              off["failed"]["said"].endswith(".")
              and not off["failed"]["said"].endswith(".."), off["failed"]["said"])
        check("and keeps the device, so it can be turned off again",
              off["failed"]["kept"] == phone, off["failed"])
        check("once the server is told, it says Off and forgets the device",
              off["then"]["told"] and off["then"]["chip"] == "Off"
              and off["then"]["kept"] == {}, off["then"])
        check("switched off in the iPhone's Settings, it is kept until the server is told",
              got["revoked"] == phone, got["revoked"])

    section("A TAPPED NOTIFICATION OPENS A PAGE OF THIS SITE, AND NEVER ANOTHER")
    # The app shows any page it is sent to with the whole bridge to the phone,
    # so a notification's url must never take it off orenth.app.
    good = {"/monitor": "https://orenth.app/monitor",
            "/analyse?symbol=NVDA&interval=1d":
                "https://orenth.app/analyse?symbol=NVDA&interval=1d",
            "/research?symbol=NVDA&interval=1d#s-news":
                "https://orenth.app/research?symbol=NVDA&interval=1d#s-news"}
    tapped = run_node(NOTIFY_TAPPED.replace(
        "__PATHS__", json.dumps(OFF_SITE + [ODD_BUT_HERE] + list(good))), notify_js)
    if tapped is None:
        print("   (Node is not installed, so notify.js is read here rather than run)")
        source = open(notify_js, encoding="utf-8").read()
        check("a tapped notification opens the full address, never a bare path",
              "window.location.assign(address)" in source
              and "pathname.slice(0, 2) === '//'" in source)
    elif "error" in tapped:
        check("notify.js runs as the app runs it", False, tapped["error"])
    else:
        for path, address in sorted(good.items()):
            check("a notification for %s opens %s" % (path, address),
                  tapped[path] == [address], tapped[path])
        for path in OFF_SITE:
            check("a notification for %r opens nothing" % path, tapped[path] == [],
                  tapped[path])
        check("one for %r stays on orenth.app" % ODD_BUT_HERE,
              tapped[ODD_BUT_HERE] == ["https://orenth.app" + ODD_BUT_HERE],
              tapped[ODD_BUT_HERE])

    section("RETIRING THE WORKER AFTER A ROLLBACK")
    # A version from before the worker answers /sw.js with "not found", and a
    # browser keeps the worker it has. Served there instead, this removes it.
    retire_js = _os.path.join(ROOT, "static", "sw-retire.js")
    retire = open(retire_js, encoding="utf-8").read() if _os.path.exists(retire_js) else ""
    check("static/sw-retire.js is there for a rolled-back version to serve", bool(retire))
    ran = run_node(WORKER_SCOPE, retire_js, "retire") if retire else None
    if retire and ran is None:
        print("   (Node is not installed, so the retiring worker is read here rather than run)")
        check("it takes over at once, deletes the kept files, removes itself and reloads",
              all(x in retire for x in ("skipWaiting", "caches.delete", "unregister()",
                                        "navigate(")) and "'fetch'" not in retire)
    elif ran is not None and "error" in ran:
        check("the retiring worker runs as a browser runs it", False, ran["error"])
    elif ran is not None:
        check("it takes over at once, rather than once every tab is closed",
              ran["skip_waiting"] >= 1, ran)
        check("deletes every file Orenth kept, and nothing of any other site's",
              ran["caches"] == ["another-site"], ran["caches"])
        check("removes itself", ran["unregistered"] == 1, ran)
        check("and reloads each Orenth page open, so it comes from the server",
              ran["navigated"] == ["https://orenth.app/monitor", "https://orenth.app/trades"],
              ran["navigated"])
        check("answering no request in the meantime", "fetch" not in ran["listens"],
              ran["listens"])
    deploy = open(_os.path.join(ROOT, "DEPLOY.md"), encoding="utf-8").read()
    route = next((m.group(1) for m in re.finditer(r"```python\n(.*?)```", deploy, re.S)
                  if "sw-retire.js" in m.group(1)), "")
    check("DEPLOY.md gives the route a rolled-back version adds to serve it", bool(route))
    if route and retire:
        from flask import Flask, send_from_directory
        # What that version has: the app, os, send_from_directory, and the
        # endpoints a browser may reach without signing in.
        rolled_back = Flask("rolled_back", root_path=ROOT)
        names = {"app": rolled_back, "os": _os, "send_from_directory": send_from_directory,
                 "PUBLIC_ENDPOINTS": set()}
        exec(textwrap.dedent(route), names)
        r = rolled_back.test_client().get("/sw.js")
        check("pasted in, it serves the retiring worker at /sw.js, as JavaScript",
              r.status_code == 200 and r.mimetype == "application/javascript"
              and r.data == open(retire_js, "rb").read(), (r.status_code, r.mimetype))
        check("checked afresh on every visit", "no-cache" in (r.headers.get("Cache-Control")
                                                             or ""), r.headers.get("Cache-Control"))
        r.close()
        served_by = [rule.endpoint for rule in rolled_back.url_map.iter_rules()
                     if rule.rule == "/sw.js"]
        check("and to a browser nobody is signed in on, which would otherwise be sent "
              "to sign in, and a worker is never taken from a redirect",
              served_by and served_by[0] in names["PUBLIC_ENDPOINTS"], served_by)
finally:
    push._web_post = REAL_WEB_POST
    web.market_mod.split_history = REAL_HISTORY
    web._call_now = REAL_CALL_NOW
    alerts.fetch_bars = REAL_FETCH
    push._CLIENT["apns"] = None
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "APPLE_TEAM_ID", "APNS_KEY_ID",
                "APNS_PRIVATE_KEY", "APNS_BUNDLE_ID", "VAPID_PRIVATE_KEY", "STOCKBOT_CONTACT"):
        _os.environ.pop(key, None)

print()
print("%d failure(s)" % len(fails))
print("PUSH OK" if not fails else "FAILURES: %s" % fails)
_sys.exit(1 if fails else 0)
