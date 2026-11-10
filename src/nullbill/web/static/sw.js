"use strict";

// NullBill's service worker: what makes the page installable as an app.
// The page comes from the network first and from this cache when the server
// cannot be reached or is slow, so an update shows at once and the app still
// opens (and says it cannot reach the server) offline. The page names its
// script and styles with a version (/app.js?v=...), and the cache holds a
// page only together with exactly the files it names: a page from one
// NullBill never runs another's script, which is what an update on a slow
// line used to leave on a phone. Nothing under /api/ is ever stored here.

const CACHE = "nullbill-shell-4";
const PLAIN = ["/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"];
const VERSIONED = /\/(?:app\.js|style\.css|theme\.js)\?v=[0-9a-f]+/g;

// Store a page the server just sent: first the files it names (and the
// icons, which the page empties this cache of when it reloads after an
// update), then the page, then drop the files of pages before it.
async function keep(response) {
  const cache = await caches.open(CACHE);
  const named = [...new Set((await response.clone().text()).match(VERSIONED) || [])];
  await Promise.all([...named, ...PLAIN].map(async (file) => (await cache.match(file)) || cache.add(file)));
  await cache.put("/", response);
  for (const request of await cache.keys()) {
    const url = new URL(request.url);
    if (url.search && !named.includes(url.pathname + url.search)) await cache.delete(request);
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(fetch("/").then((response) => (response.ok ? keep(response) : null)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// On a slow line the cached copy answers after WAIT ms, and the network's
// answer still refreshes the cache for next time.
const WAIT = 4000;

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  const name = url.pathname + url.search;
  if (name.match(VERSIONED)?.[0] === name) {
    // A versioned file never changes: the stored one is the right one.
    event.respondWith(caches.match(name).then((cached) => cached || fetch(event.request)));
    return;
  }
  if (name !== "/" && !PLAIN.includes(name)) return;
  const network = fetch(event.request);
  const stored = network.then((response) => {
    if (!response.ok) return null;
    const copy = response.clone();
    return name === "/" ? keep(copy) : caches.open(CACHE).then((cache) => cache.put(name, copy));
  });
  event.waitUntil(stored.catch(() => {}));
  event.respondWith(new Promise((resolve) => {
    let done = false;
    const answer = (response) => { if (!done && response) { done = true; resolve(response); } };
    const cached = () => caches.match(name);
    const timer = setTimeout(() => cached().then(answer), WAIT);
    network.then((response) => { clearTimeout(timer); answer(response); })
      .catch(() => { clearTimeout(timer); cached().then((c) => (done ? null : resolve(c || Response.error()))); });
  }));
});
