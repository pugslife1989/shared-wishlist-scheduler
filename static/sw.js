/* Places to Go service worker — v2, app-shell only.
   Caches only the static shell (manifest, icons). It does NOT cache
   /api/*, /login, /signup, /photos/* or /calendar/*.ics, so lists,
   logins and calendar feeds always go to the network. If offline,
   the shell still loads; data calls will fail until you're back online. */
const CACHE = "places-to-go-v2";
const SHELL = [
  "/",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Only handle same-origin GETs for the app shell.
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/photos/") ||
      url.pathname.startsWith("/calendar/") || url.pathname === "/login" ||
      url.pathname === "/signup" || url.pathname === "/logout") return;
  // Network first for pages, cache fallback for the shell.
  event.respondWith(
    fetch(event.request).then((resp) => {
      if (resp.ok && SHELL.includes(url.pathname)) {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
      }
      return resp;
    }).catch(() => caches.match(event.request))
  );
});
