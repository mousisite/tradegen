/* Orenth's service worker, retired.

   For a version of Orenth from before the worker, put back by a rollback,
   to serve at /sw.js in place of the real one: DEPLOY.md, "Rolling back to a
   version without the offline worker", says how. A browser asks for /sw.js
   each time it opens an Orenth page, and puts this in place of the worker
   it had. This deletes the files Orenth kept in that browser, removes
   itself, and reloads each Orenth page open there, so they come straight
   from the server. It answers no requests while it is in place. Removing
   the worker also ends the notifications that browser had switched on. */
'use strict';

self.addEventListener('install', (event) => {
  // In place at once, rather than once every Orenth tab has been closed.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith('orenth-'))
      .map((name) => caches.delete(name)));
    await self.registration.unregister();
    const open = await self.clients.matchAll({ type: 'window' });
    await Promise.all(open.map((client) => (typeof client.navigate === 'function'
      ? client.navigate(client.url).catch(() => null) : null)));
  })());
});
