/* 白头凤 PWA Service Worker —— 资源本地化层
 *
 * 目标：手机端第二次打开不再走网络瀑布（69 个 .ts 模块 + vendor 库）。
 *
 * 策略分工（按请求类型）：
 *   - 导航请求（HTML）      : network-first —— 后端改了路由/模板立刻可见，断网回落缓存
 *   - /static/ 静态资源     : stale-while-revalidate —— 命中缓存立即返回，同时后台静默更新
 *   - API / WS / 生成内容   : 完全不拦截 —— 实时数据交给后端，缓存只会帮倒忙
 *
 * 开发机（localhost / 127.0.0.1）例外：静态资源改走 network-first，
 * 否则本地改一行前端代码要刷新两次才看到，开发体验会被这层缓存毁掉。
 *
 * 逃生门：任意页面 URL 带 ?fresh=1 会清空全部缓存并注销自己（调试用）。
 */

/* 版本号必须跟前端代码一起动：activate 会删掉非当前版本的整个 CacheStorage。
 * 只改 index.html 的 ?v= 是不够的 —— 只有入口 app.ts 带版本号，
 * 它 import 的 69 个子模块（如 js/ui/30_task_big_screen.ts）URL 不带版本，
 * 在 SWR 下缓存 key 永不变 → 改子模块后第一次打开仍拿到旧文件（后台才更新）。
 * 升这个号 = 强制清缓存，一次重拉（含 24MB VRM，局域网几秒），换后续全部新鲜。 */
const SW_VERSION = '6';
const CACHE = `dabai-shell-v${SW_VERSION}`;

// 首屏骨架：装完 SW 就有离线可用的底子。
// 刻意不放 /static/*：index.html 里的 ?v= 版本号与这里硬编码的值是隐式耦合，
// 哪天改了 v 号而忘了同步，precache 会静默 404。这些资源首次渲染时本来就会被
// 请求并经 staleWhileRevalidate 落缓存，precache 它们纯属冗余。
const PRECACHE = [
  '/',
  '/manifest.webmanifest',
];

// 这些前缀下的内容一律不碰（实时数据 / 用户生成物 / 大屏推送）
const BYPASS_PREFIXES = ['/api/', '/ws/', '/generated/', '/downloads/', '/mpt/', '/anim/'];

// 单条缓存上限：24MB 的 VRM 必须放得进来（CacheStorage 配额通常是可用磁盘的百分之几，
// 40MB 单条在 iOS/Android 上都可写）。超限的响应静默跳过，不影响本次渲染。
const MAX_CACHE_BYTES = 40 * 1024 * 1024;

// 内容不变的大资产：命中缓存直接返回，不发起任何后台请求。
// 它们走 staleWhileRevalidate 是个陷阱——SWR 每次打开都会在后台把 24MB 重下一遍，
// 走公网隧道时直接吃满上行带宽，页面其余请求全被拖慢。
const IMMUTABLE_PREFIXES = ['/models/', '/backgrounds/', '/audio/'];

const isDev = () => {
  const h = self.location.hostname;
  return h === 'localhost' || h === '127.0.0.1' || h === '::1';
};

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // 逐个 add，单个失败不拖垮整体（离线安装时 style.css 可能取不到）
    await Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('dabai-') && k !== CACHE).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 逃生门：清空全部缓存 + 注销自身，本次请求直连网络（调试用）
  if (url.searchParams.get('fresh') === '1') {
    event.waitUntil(resetAll());
    return;
  }
  if (BYPASS_PREFIXES.some((p) => url.pathname.startsWith(p))) return;
  // Range 请求（音视频拖动进度）缓存会返回不完整内容
  if (req.headers.get('range')) return;
  if (req.headers.get('upgrade') === 'websocket') return;

  if (req.mode === 'navigate') {
    event.respondWith(networkFirst(req));
    return;
  }

  if (IMMUTABLE_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(cacheFirst(req));
    return;
  }

  if (isStaticAsset(url.pathname)) {
    // 代码类资源（.ts/.js/.css/.html，除 vendor）一律 network-first：
    // 服务端已对它们返回 cache-control: no-cache + ETag，回源最多换一个 304（几百字节），
    // 而 SWR 会先吐旧文件、后台才更新 —— 表现就是「改了代码刷新还是旧的」，
    // 必须再刷一次才生效。靠人记得升版本号不靠话，这里直接消除这类陷阱。
    event.respondWith(isDev() || isCodeAsset(url.pathname) ? networkFirst(req) : staleWhileRevalidate(req));
  }
});

// 代码类资源：体积几 KB 且服务端带 ETag，回源最多换一个 304，不该被缓存延迟一次。
// vendor/ 是第三方库（three / pixiv / html2canvas），内容不变，继续走 SWR 省流量。
// 注意 pathname 不含 query，所以只需匹配结尾扩展名。
function isCodeAsset(pathname) {
  if (pathname.startsWith('/static/vendor/')) return false;
  return /\.(ts|js|mjs|css|html)$/i.test(pathname);
}

function isStaticAsset(pathname) {
  if (pathname.startsWith('/static/')) return true;
  if (pathname.startsWith('/models/')) return true;
  if (pathname.startsWith('/backgrounds/')) return true;
  if (pathname.startsWith('/audio/')) return true;
  return /\.(css|js|mjs|ts|json|webmanifest|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|mp3|wav|ogg|glb|gltf|vrm|bin|ktx2|hdr|exr)$/i.test(pathname);
}

async function putSafe(cache, req, res) {
  if (!res || res.status !== 200 || res.type !== 'basic') return;
  const len = Number(res.headers.get('content-length') || 0);
  if (len > MAX_CACHE_BYTES) return;
  try {
    await cache.put(req, res.clone());
  } catch {
    /* 配额满：静默放弃，不影响本次响应 */
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req, { ignoreSearch: false });

  const network = fetch(req)
    .then(async (res) => {
      await putSafe(cache, req, res);
      return res;
    })
    .catch(() => null);

  if (cached) {
    // 不 await：让后台更新继续跑，页面立刻拿到缓存
    network.catch(() => {});
    return cached;
  }

  const res = await network;
  if (res) return res;

  const fallback = await cache.match(req, { ignoreSearch: true });
  if (fallback) return fallback;
  return new Response('资源暂不可用（离线且未缓存）', {
    status: 504,
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

// 不变资产（VRM/贴图/音频）：缓存优先。命中即返回，一次网络请求都不发——
// 这是手机端第二次打开能秒进的关键。
async function cacheFirst(req) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;

  let res;
  try {
    res = await fetch(req);
  } catch {
    return new Response('资源暂不可用（离线且未缓存）', {
      status: 504,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
  // 不 await：24MB 写盘要几秒，不能拖慢首屏；写失败也不影响本次渲染
  putSafe(cache, req, res.clone()).catch(() => {});
  return res;
}

async function resetAll() {
  const keys = await caches.keys();
  await Promise.all(
    keys.filter((k) => k.startsWith('dabai-')).map((k) => caches.delete(k))
  );
  await self.registration.unregister();
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    await putSafe(cache, req, res);
    return res;
  } catch {
    const cached = await cache.match(req);
    if (cached) return cached;
    if (req.mode === 'navigate') {
      const shell = await cache.match('/');
      if (shell) return shell;
    }
    return new Response('离线：白头凤未启动或不在同一网络', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}
