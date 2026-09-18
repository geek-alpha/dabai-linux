/* PWA 真实链路探针：无头 Chromium + CDP 实测 Service Worker 是否注册、缓存是否落地。
 *
 * 用法：node tools/sw_probe.mjs <url> [--keep]
 * 退出码：0=诊断跑通（结论看输出），1=连不上浏览器/协议失败。
 *
 * 为什么不用 --dump-dom：--virtual-time-budget 会冻结虚拟时钟，SW 注册这类
 * 依赖真实网络往返的异步流程永远停在 pending，实测拿到的是假阴性。
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const url = process.argv[2];
if (!url) {
  console.error('用法: node tools/sw_probe.mjs <url>');
  process.exit(1);
}
const PORT = 9333;
const profile = mkdtempSync(join(tmpdir(), 'swprobe-'));

const chrome = spawn('chromium', [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-dev-shm-usage',
  '--ignore-certificate-errors',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: ['ignore', 'ignore', 'ignore'] });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForDevtools(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return await r.json();
    } catch { /* 还没起来 */ }
    await sleep(250);
  }
  throw new Error('devtools 端口未就绪');
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.sessionId = null; }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && c.pending.has(msg.id)) {
        const { res, rej } = c.pending.get(msg.id);
        c.pending.delete(msg.id);
        msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
      }
    };
    return c;
  }
  send(method, params = {}, sessionId = this.sessionId) {
    const id = ++this.id;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    this.ws.send(JSON.stringify(payload));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
}

let exitCode = 0;
try {
  const version = await waitForDevtools();
  const cdp = await CDP.connect(version.webSocketDebuggerUrl);

  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  cdp.sessionId = sessionId;
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  // 首访：让页面自己的注册逻辑跑起来
  await cdp.send('Page.navigate', { url });
  await sleep(12000);

  const DIAG = `(async () => {
    const out = [];
    out.push('secureContext=' + window.isSecureContext);
    out.push('swSupported=' + ('serviceWorker' in navigator));
    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      if (!reg) { out.push('registration=NONE'); }
      else {
        out.push('scope=' + reg.scope);
        out.push('active=' + !!reg.active);
        out.push('controller=' + !!navigator.serviceWorker.controller);
      }
    } catch (e) { out.push('REG_ERR ' + e.name + ': ' + e.message); }
    try {
      const keys = await caches.keys();
      out.push('caches=' + JSON.stringify(keys));
      let total = 0;
      for (const k of keys) {
        const c = await caches.open(k);
        const reqs = await c.keys();
        total += reqs.length;
        if (reqs.length) {
          out.push('[' + k + '] n=' + reqs.length + ' 例:' +
            reqs.slice(0, 6).map(r => r.url.replace(location.origin, '')).join(','));
        }
      }
      out.push('cachedEntries=' + total);
    } catch (e) { out.push('CACHE_ERR ' + e.name + ': ' + e.message); }
    return out.join('\\n');
  })()`;

  const r1 = await cdp.send('Runtime.evaluate', {
    expression: DIAG, awaitPromise: true, returnByValue: true,
  });
  console.log('===== 首访后状态 =====');
  console.log(r1.result.value);

  // 二访：统计本次实际走网络的请求数（缓存命中的不进 network 域）
  const NET = `(() => { window.__net = 0;
    new PerformanceObserver(() => {}).observe({ entryTypes: ['resource'] });
    return performance.getEntriesByType('resource').length; })()`;
  await cdp.send('Runtime.evaluate', { expression: NET, returnByValue: true });

  const t0 = Date.now();
  await cdp.send('Page.reload', { ignoreCache: false });
  await sleep(10000);
  const r2 = await cdp.send('Runtime.evaluate', {
    expression: `(() => {
      const rs = performance.getEntriesByType('resource');
      const nav = performance.getEntriesByType('navigation')[0] || {};
      return JSON.stringify({
        resourceCount: rs.length,
        transferKB: Math.round(rs.reduce((a, r) => a + (r.transferSize || 0), 0) / 1024),
        decodedKB: Math.round(rs.reduce((a, r) => a + (r.decodedBodySize || 0), 0) / 1024),
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd || 0),
        loadMs: Math.round(nav.loadEventEnd || 0),
        controller: !!navigator.serviceWorker.controller,
      });
    })()`,
    awaitPromise: true, returnByValue: true,
  });
  console.log('===== 二次加载（SW 已接管）=====');
  console.log(r2.result.value);
  console.log('wallMs=' + (Date.now() - t0));
} catch (e) {
  console.error('探针失败: ' + e.message);
  exitCode = 1;
} finally {
  chrome.kill('SIGKILL');
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* 清理失败无所谓 */ }
  process.exit(exitCode);
}
