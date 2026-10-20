self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(clients.claim()));
// Outside the app only the icon badge shows how many tabs are waiting: no banner. The browser insists that every push is
// shown as a notification, so each one is closed again straight away; the waiting tabs are counted in a small cache instead.
const WAITING = 'waiting-tabs';
async function waitingCount(change) {
  const cache = await caches.open(WAITING);
  if (change?.add) await cache.put('/mobile/waiting/' + encodeURIComponent(change.add), new Response('1'));
  if (change?.clear) for (const key of await cache.keys()) await cache.delete(key);
  const count = (await cache.keys()).length;
  try { await (count ? self.navigator.setAppBadge(count) : self.navigator.clearAppBadge()); } catch {}
}
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data.json(); } catch {}
  event.waitUntil((async () => {
    const tag = data.tag || 'tmux-mobile';
    await waitingCount({add: tag});
    await self.registration.showNotification(data.title || 'Tmux Mobile', {
      body: data.body || '', tag, renotify: false, silent: true, icon: '/mobile/icon-192.png', badge: '/mobile/icon-192.png',
      data: {url: data.url || '/mobile/'}});
    for (const note of await self.registration.getNotifications({tag})) note.close();
  })());
});
self.addEventListener('message', event => { if (event.data?.clear) event.waitUntil((async () => {
  for (const note of await self.registration.getNotifications()) note.close();
  await waitingCount({clear: true});
})()); });
self.addEventListener('notificationclick', event => {
  event.notification.close();
    const url = event.notification.data?.url || '/mobile/';
  event.waitUntil((async () => {
    const session = decodeURIComponent((url.split('session=')[1] || '').split('&')[0]);
    for (const client of await clients.matchAll({type: 'window', includeUncontrolled: true})) {
      if (new URL(client.url).pathname.startsWith('/mobile/')) {
        await client.focus();
        if (session) client.postMessage({session});
        return;
      }
    }
    await clients.openWindow(url);
  })());
});
