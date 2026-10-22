// Retired service worker (v1.3).
//
// The old worker answered every page, script and style "cache first" under a
// cache name that never changed, so after an update the app kept showing the
// previous version's UI, and it also stored /api/* responses on disk. The UI
// is served by the local server on this same machine, so offline caching buys
// nothing. Browsers re-check /sw.js on navigation: this version installs over
// the old one, deletes every cache, unregisters itself and reloads open pages
// so they get the current UI.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.registration.unregister();
    const pages = await self.clients.matchAll({ type: 'window' });
    for (const page of pages) page.navigate(page.url);
  })());
});
