/* Control de Flota - Service Worker
   Precachea la app al instalar para que funcione SIN conexión.
   Funciona con cualquier nombre de página (index.html, mantenimiento-flota.html, etc.).
   Sube este archivo en la MISMA carpeta que tu HTML. */

const CACHE = 'flota-cache-v2';

// Recursos externos (fuentes). Tolerante a fallos: si no cargan, no rompe la instalacion.
const EXTRA = [
  'https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;800;900&family=Archivo+Narrow:wght@600;700&family=JetBrains+Mono:wght@400;600&display=swap'
];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // No asumimos ningun nombre de archivo concreto: cacheamos la raiz del scope
    // y la propia pagina. Todo tolerante a fallos para que un 404 nunca impida
    // que el service worker se instale.
    const scope = self.registration.scope;
    const candidates = [scope, './'];
    await Promise.allSettled(candidates.map(u => cache.add(u)));
    await Promise.allSettled(EXTRA.map(u => cache.add(u)));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  // Navegacion (abrir la app): intenta red; si no hay, sirve la copia cacheada
  // de ESA misma pagina (sea cual sea su nombre), o la raiz del scope como respaldo.
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      const cache = await caches.open(CACHE);
      try {
        const net = await fetch(req);
        cache.put(req, net.clone());
        return net;
      } catch (err) {
        const exact = await cache.match(req);
        if (exact) return exact;
        const root = await cache.match(self.registration.scope);
        if (root) return root;
        const slash = await cache.match('./');
        if (slash) return slash;
        const all = await cache.keys();
        for (const k of all) {
          const r = await cache.match(k);
          if (r && (r.headers.get('content-type') || '').includes('text/html')) return r;
        }
        return Response.error();
      }
    })());
    return;
  }

  // Resto de recursos: cache-first con actualizacion en segundo plano.
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
