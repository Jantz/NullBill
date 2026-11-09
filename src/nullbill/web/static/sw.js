"use strict";

// NullBill's service worker: what makes the page installable as an app.
// The page's own files come from the network first and from this cache
// only when the server cannot be reached, so an update shows at once and
// the app still opens (and says it cannot reach the server) when offline.
// Nothing under /api/ is ever stored here.

const CACHE = "nullbill-shell-3";
const SHELL = ["/", "/app.js", "/style.css", "/theme.js", "/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png",
  "/icon-maskable-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(url.pathname, copy));
    }
    return response;
  }).catch(() => caches.match(url.pathname).then((cached) => cached || Response.error())));
});
