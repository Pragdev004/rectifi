// Rectifi service worker. It does one job: show a notification when the laptop sends a push, and open Rectifi when you tap it.
// It never touches the network and never stores any page, so there is nothing here that can go stale or serve old money figures:
// no request is intercepted and nothing is written to the browser cache. On every start it also deletes any cache an earlier
// version might have left behind.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

// The address in a push is a path inside Rectifi ("/alerts#12"). Anything else becomes the alerts page.
function target(path) {
  try {
    const url = new URL(typeof path === "string" ? path : "/alerts", self.location.origin);
    return url.origin === self.location.origin ? url.href : new URL("/alerts", self.location.origin).href;
  } catch (e) {
    return new URL("/alerts", self.location.origin).href;
  }
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }
  const title = typeof data.t === "string" && data.t ? data.t : "Rectifi";
  const body = typeof data.b === "string" ? data.b : "";
  const options = { body, data: { u: typeof data.u === "string" ? data.u : "/alerts" }, icon: "/icon-192.png" };
  if (typeof data.tag === "string" && data.tag) options.tag = data.tag;
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const href = target(event.notification.data && event.notification.data.u);
  event.waitUntil(
    (async () => {
      const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of open) {
        if (new URL(client.url).origin !== self.location.origin) continue;
        await client.focus();
        if ("navigate" in client) await client.navigate(href);
        return;
      }
      await self.clients.openWindow(href);
    })(),
  );
});
