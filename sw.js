/* GL PANELS · service worker (FASE 9.4)
 *
 * Ámbito: la carpeta donde está este archivo (en 2.0: /TINTA-SAPPHIC-GL-PANELS-2.0/). No controla la versión oficial.
 * Las cachés llevan el ámbito en el nombre («glp:<ámbito>:…») y solo se limpian las de ESTE ámbito.
 *
 * QUÉ SE GUARDA (todo público, igual para todas las personas):
 *   shell   · index.html (siempre fresco con red; la copia solo es respaldo sin red), manifest e iconos
 *   data    · manga_data.json y official_links.json (con red: siempre la versión más nueva; sin red: la última copia)
 *   static  · assets/ (iconos de logros), Google Fonts y la librería supabase-js
 *
 * QUÉ NO SE TOCA NUNCA (pasa directo a la red, el service worker ni lo ve):
 *   · Supabase (*.supabase.co): sesión, votos, estantería, calendario, días de lectura…
 *   · cualquier petición que no sea GET, o que lleve cabecera Authorization
 *   · portadas de terceros, admin.html y cualquier otro archivo no listado arriba
 *
 * LENTITUD: la página y la librería usan la copia guardada si la red tarda más de 5 s; los datos del catálogo, nunca.
 *
 * ACTUALIZACIÓN: un service worker nuevo se instala en segundo plano y ESPERA; la página avisa y la persona
 * decide («Actualizar»). Sube VERSION cuando cambies este archivo o la lista PRECACHE.
 */
'use strict';

const VERSION = '3';
const SCOPE_URL = new URL('./', self.location).href;          // https://…/TINTA-SAPPHIC-GL-PANELS-2.0/
const SCOPE = new URL(SCOPE_URL).pathname;                      // /TINTA-SAPPHIC-GL-PANELS-2.0/
const PFX = 'glp:' + SCOPE + ':';
const C_SHELL = PFX + 'shell:v' + VERSION;
const C_DATA = PFX + 'data:v' + VERSION;
const C_STATIC = PFX + 'static:v' + VERSION;
const CURRENT = [C_SHELL, C_DATA, C_STATIC];

const PRECACHE = ['manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/favicon-32.png'];
const DATA_FILES = ['manga_data.json', 'official_links.json'];
const NET_TIMEOUT_MS = 5000;   // con copia guardada y red lenta: se muestra la copia (y se actualiza en segundo plano)
const EXTERNAL_STATIC = ['fonts.googleapis.com', 'fonts.gstatic.com'];
const EXTERNAL_LIB = ['cdn.jsdelivr.net'];

self.addEventListener('install', e => {
  // No se llama a skipWaiting() aquí: la actualización espera a que la persona la acepte.
  e.waitUntil(
    // Tolerante: si un archivo de la lista falta en el servidor, el service worker se instala igual (con addAll() un solo 404
    // lo impediría y el navegador dejaría de ofrecer «Instalar»). Lo que no se pueda guardar se pide a la red cuando haga falta.
    caches.open(C_SHELL).then(c => Promise.all(PRECACHE.map(u => c.add(new Request(u, { cache: 'reload' })).catch(() => {}))))
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    // Solo se borran cachés de este ámbito que ya no son las vigentes. Las demás (otras webs, la oficial) no se tocan.
    const names = await caches.keys();
    await Promise.all(names.filter(n => n.startsWith(PFX) && !CURRENT.includes(n)).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || req.headers.has('authorization')) return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (!url.pathname.startsWith(SCOPE)) return;
    const rel = url.pathname.slice(SCOPE.length);
    // Solo la página principal. admin.html y demás páginas quedan fuera (nunca se guardan).
    if (req.mode === 'navigate') {
      if (rel === '' || rel === 'index.html') e.respondWith(handleNavigate(e, req));
      return;
    }
    if (DATA_FILES.includes(rel)) { e.respondWith(networkFirst(e, req, C_DATA, new Request(url.origin + url.pathname), true)); return; }
    if (rel === 'manifest.webmanifest' || rel.startsWith('icons/')) { e.respondWith(staleWhileRevalidate(e, req, C_SHELL)); return; }
    if (rel.startsWith('assets/')) { e.respondWith(staleWhileRevalidate(e, req, C_STATIC)); return; }
    return;
  }

  if (EXTERNAL_STATIC.includes(url.hostname)) { e.respondWith(staleWhileRevalidate(e, req, C_STATIC)); return; }
  if (EXTERNAL_LIB.includes(url.hostname)) { e.respondWith(networkFirst(e, req, C_STATIC, req, false)); return; }
  // Supabase y todo lo demás: sin respondWith → va directo a la red
});

function okToStore(res) { return res && (res.ok || res.type === 'opaque'); }

async function handleNavigate(e, req) {
  // La página es la misma con cualquier ?query: se guarda una sola copia bajo la URL del ámbito.
  const key = new Request(SCOPE_URL);
  const cache = await caches.open(C_SHELL);
  const net = fetch(new Request(req.url, { cache: 'no-cache', credentials: 'same-origin' })).then(res => {
    if (res.ok && res.type === 'basic') cache.put(key, res.clone());
    return res;
  });
  const cached = await cache.match(key);
  if (!cached) return net.catch(() => offlinePage());
  e.waitUntil(net.catch(() => {}));
  return Promise.race([net.catch(() => cached), delay(NET_TIMEOUT_MS).then(() => cached)]);
}

async function networkFirst(e, req, cacheName, key, revalidate) {
  const cache = await caches.open(cacheName);
  const net = fetch(revalidate ? new Request(req.url, { cache: 'no-cache' }) : req).then(res => {
    if (okToStore(res)) cache.put(key, res.clone());
    return res;
  });
  const cached = await cache.match(key);
  if (!cached) return net.catch(() => Response.error());
  e.waitUntil(net.catch(() => {}));
  // Datos del catálogo (revalidate): la copia guardada SOLO se usa si la red falla de verdad, nunca por lentitud.
  // Así el panel de administración (u otra página) no lee datos viejos con una conexión lenta.
  if (revalidate) return net.catch(() => cached);
  return Promise.race([net.catch(() => cached), delay(NET_TIMEOUT_MS).then(() => cached)]);
}

async function staleWhileRevalidate(e, req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const net = fetch(req).then(res => { if (okToStore(res)) cache.put(req, res.clone()); return res; }).catch(() => null);
  if (cached) { e.waitUntil(net); return cached; }
  return (await net) || Response.error();
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

function offlinePage() {
  const html = '<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>GL PANELS · sin conexión</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#111118;color:#f5f0e8;font:16px/1.6 system-ui,sans-serif;text-align:center;padding:24px">' +
    '<main><h1 style="font-weight:400;font-size:2rem;margin:0 0 .5rem">GL PANELS</h1><p>Sin conexión y todavía no hay una copia guardada.<br>Conéctate una vez para poder abrirla después sin red.</p>' +
    '<button onclick="location.reload()" style="margin-top:1rem;padding:.6rem 1.2rem;border-radius:999px;border:1px solid #e8ff47;background:#e8ff47;color:#0a0a0a;font-weight:700;cursor:pointer">Reintentar</button></main></body></html>';
  return new Response(html, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}
