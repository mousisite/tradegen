/* Orenth's service worker, served from /sw.js so it covers the whole site.

   What it does:
   - keeps a copy of the public files every page uses (the style sheet and
     the icons) and of the "no connection" page, so the app still opens, and
     says plainly what is wrong, when there is no connection;
   - shows the alert notifications the server pushes, and opens Orenth on
     the right page when one is tapped.

   What it never does is keep a page, or anything from /api. Those hold one
   person's journal, alerts and account, and a phone is often shared: every
   page comes from the network, every time, and only if the network fails is
   the offline page shown instead.

   Made by web.py from templates/sw.js. The version changes whenever a file
   it keeps changes, which gives it a new cache and removes the old one. */
'use strict';

const VERSION = {{ version|tojson }};
const CACHE = 'orenth-' + VERSION;
const OFFLINE = {{ offline|tojson }};
const PRECACHE = {{ precache|tojson }};
const STATIC = {{ static_prefix|tojson }};
const ICON = {{ icon|tojson }};
const BADGE = {{ badge|tojson }};
const HOME = {{ home|tojson }};
const CONFIG = {{ config_url|tojson }};
const SUBSCRIBE = {{ subscribe_url|tojson }};

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // Straight from the server rather than the browser's own cache, so a new
    // version is not filled with the old files, and without cookies, so the
    // offline page kept here is the one anybody would get.
    await cache.addAll(PRECACHE.concat([OFFLINE]).map(
      (url) => new Request(url, { cache: 'reload', credentials: 'omit' })));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith('orenth-') && name !== CACHE)
      .map((name) => caches.delete(name)));
    // Lets the page's request start while the worker wakes, rather than after.
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) {}
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  // A form being sent, and anything to another site, is left to the browser.
  if (request.method !== 'GET') { return; }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) { return; }
  if (request.mode === 'navigate') {
    event.respondWith(page(event));
    return;
  }
  if (url.pathname.startsWith(STATIC)) {
    event.respondWith(file(request));
  }
  // Everything else, /api included, goes to the network untouched.
});

// A page: always from the network, never kept. Only when the network fails
// is the offline page shown, under the address that was asked for, so
// "Try again" asks for that address again.
async function page(event) {
  try {
    const early = await event.preloadResponse;
    if (early) { return early; }
    return await fetch(event.request);
  } catch (err) {
    const cache = await caches.open(CACHE);
    return (await cache.match(OFFLINE)) || Response.error();
  }
}

// A public file: from the kept copy when there is one, otherwise from the
// network, keeping a copy. Only ever something under /static/, and never
// anything that answers as a page or as data, whatever its address says.
async function file(request) {
  const cache = await caches.open(CACHE);
  const kept = await cache.match(request);
  if (kept) { return kept; }
  const fresh = await fetch(request);
  if (fresh.ok && fresh.type === 'basic' && keepable(fresh)) {
    cache.put(request, fresh.clone()).catch(() => {});
  }
  return fresh;
}

function keepable(response) {
  const type = (response.headers.get('Content-Type') || '').toLowerCase();
  return !type.includes('html') && !type.includes('json');
}

// Only a path on this site, so a notification can never open somewhere else.
function samePath(raw) {
  if (typeof raw !== 'string' || raw.charAt(0) !== '/' || raw.charAt(1) === '/'
      || raw.indexOf('\\') !== -1) {
    return HOME;
  }
  try {
    const url = new URL(raw, self.location.origin);
    return url.origin === self.location.origin
      ? url.pathname + url.search + url.hash : HOME;
  } catch (e) {
    return HOME;
  }
}

self.addEventListener('push', (event) => {
  let note = {};
  if (event.data) {
    try { note = event.data.json(); } catch (e) { note = { body: event.data.text() }; }
  }
  const tag = String(note.tag || 'orenth');
  event.waitUntil(self.registration.showNotification(
    String(note.title || 'Orenth').slice(0, 120), {
      body: String(note.body || '').slice(0, 1500),
      icon: ICON,
      badge: BADGE,
      // The same alert firing again replaces its last notification rather
      // than stacking up beside it, and still makes a sound.
      tag: tag,
      renotify: true,
      data: { url: samePath(note.url) },
    }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(samePath(event.notification.data && event.notification.data.url),
                         self.location.origin).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of open) {
      if (new URL(client.url).origin !== self.location.origin) { continue; }
      try {
        const shown = await client.focus();
        if (shown && 'navigate' in shown) { await shown.navigate(target); }
        return;
      } catch (e) {
        break;                  // not one this worker may steer: open a new one
      }
    }
    await self.clients.openWindow(target);
  })());
});

// The push service replaced or expired this browser's subscription. A new
// one is made with the server's current key and the server is told, so
// alerts keep arriving without anyone visiting the page. Sent as a check:
// the server keeps it only in place of a device it already had for whoever
// is signed in here, never as a device switched on afresh.
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription || null;
    let fresh = event.newSubscription || null;
    if (!fresh) {
      let key = null;
      try {
        const answer = await fetch(CONFIG, { credentials: 'same-origin' });
        const config = await answer.json();
        if (config.web && config.web.enabled) { key = bytes(config.web.public_key); }
      } catch (e) {}
      key = key || (old && old.options && old.options.applicationServerKey) || null;
      if (!key) { return; }
      fresh = await self.registration.pushManager.subscribe(
        { userVisibleOnly: true, applicationServerKey: key });
    }
    await fetch(SUBSCRIBE, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'web', subscription: fresh.toJSON(), check: true,
                             replaces: old ? old.endpoint : '' }),
    });
  })());
});

function bytes(text) {
  const plain = (text + '='.repeat((4 - text.length % 4) % 4))
    .replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(plain), (c) => c.charCodeAt(0));
}
