/* Control de Flota - Service Worker
   Precachea la app al instalar para que funcione SIN conexión.
   Sube este archivo junto a index.html en la misma carpeta. */

const CACHE = 'flota-cache-v1';

// Recursos propios de la app (mismo origen). Se cachean al instalar.
const CORE = [
  './',
  './index.html'
];

// Recursos externos (fuentes). Se intentan cachear, pero si fallan no rompen la instalación.
const EXTRA = [
  'https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;800;900&family=Archivo+Narrow:wght@600;700&family=JetBrains+Mono:wght@400;600&display=swap'
];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // los propios deben cachearse sí o sí
    await cache.addAll(CORE);
    // los externos, de forma tolerante a fallos
    await Promise.allSettled(EXTRA.map(u => cache.add(u)));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    // limpiar versiones antiguas de caché
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  // Navegación (abrir la app): intenta red y cae a la copia cacheada de index.html.
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const net = await fetch(req);
        const cache = await caches.open(CACHE);
        cache.put('./index.html', net.clone());
        return net;
      } catch {
        const cache = await caches.open(CACHE);
        return (await cache.match('./index.html')) || (await cache.match('./')) || Response.error();
      }
    })());
    return;
  }

  // Resto de recursos: cache-first con actualización en segundo plano.
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req);
    const net = fetch(req).then(r => {
      if (r && r.status === 200 && (r.type === 'basic' || r.type === 'cors')) {
        cache.put(req, r.clone());
      }
      return r;
    }).catch(() => null);
    return hit || (await net) || Response.error();
  })());
});
