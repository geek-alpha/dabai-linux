#!/usr/bin/env node
/* 常驻浏览器控制 daemon —— 给「跨轮交互」的流程用（登录、表单、等人工验证码）。
   playwright 技能是一次性进程，跑完即退，保不住会话，所以这里单独常驻一个。

   启动：NODE_PATH=$(npm root -g) setsid nohup node browser_daemon.js > ../run/daemon.log 2>&1 &
   控制：curl -s -X POST http://127.0.0.1:8123/cmd -d '{"action":"open","url":"https://x"}'
   action: status open dump fill click press shot text close
*/
const http = require('http');
const fs = require('fs');
const { chromium } = require('playwright');

const PORT = Number(process.env.PW_DAEMON_PORT || 8123);
const ROOT = process.env.PW_ROOT || '/root/dabai/run';
const PROFILE = process.env.PW_PROFILE || ROOT + '/browser_profile';
const UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

let ctx = null;
let page = null;

const log = (...a) => console.log(new Date().toISOString(), ...a);

async function ensure() {
  if (ctx) {
    if (!page || page.isClosed()) {
      const alive = ctx.pages().filter((p) => !p.isClosed());
      page = alive.length ? alive[alive.length - 1] : await ctx.newPage();
    }
    return page;
  }
  fs.mkdirSync(PROFILE, { recursive: true });
  ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: !process.env.PW_HEADFUL,
    viewport: { width: 1280, height: 900 },
    userAgent: UA,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
  });
  ctx.on('page', (p) => {
    log('new page', p.url().slice(0, 100));
    page = p;
  });
  page = ctx.pages()[0] || (await ctx.newPage());
  return page;
}

// 主 frame 找不到就逐 frame 找，魔搭这类页面常把登录塞进 iframe
async function locate({ selector, text }) {
  const p = await ensure();
  const frames = p.frames();
  if (selector) {
    for (const f of frames) {
      const loc = f.locator(selector);
      const n = await loc.count().catch(() => 0);
      if (n > 0) return { frame: f, loc: loc.first(), count: n };
    }
    return null;
  }
  if (text) {
    for (const f of frames) {
      const loc = f.getByText(text, { exact: false });
      const n = await loc.count().catch(() => 0);
      if (n > 0) return { frame: f, loc: loc.first(), count: n };
    }
  }
  return null;
}

const CMDS = {
  async status() {
    const p = await ensure();
    let title = '';
    try {
      title = await p.title();
    } catch (_) {}
    return { running: !!ctx, url: p.url(), title, profile: PROFILE };
  },

  async open({ url, wait = 3000 }) {
    if (!url) throw new Error('url 必填');
    const p = await ensure();
    const resp = await p.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForTimeout(wait);
    return { status: resp && resp.status(), url: p.url(), title: await p.title() };
  },

  async dump() {
    const p = await ensure();
    const out = { url: p.url(), title: await p.title(), frames: [] };
    for (const f of p.frames()) {
      const info = { url: f.url().slice(0, 140), name: f.name(), elements: [] };
      try {
        info.elements = await f.evaluate(() => {
          const res = [];
          document
            .querySelectorAll('input,button,a,textarea,select,[role=button],[class*=code],[class*=send]')
            .forEach((el) => {
              const r = el.getBoundingClientRect();
              const st = getComputedStyle(el);
              if (!(r.width > 0 && r.height > 0) || st.visibility === 'hidden' || st.display === 'none') return;
              res.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                id: el.id || '',
                name: el.name || '',
                ph: el.placeholder || '',
                cls: String(el.className || '').slice(0, 48),
                txt: (el.innerText || el.value || '').trim().slice(0, 30),
              });
            });
          return res.slice(0, 60);
        });
      } catch (e) {
        info.error = String(e.message).slice(0, 120);
      }
      out.frames.push(info);
    }
    return out;
  },

  async fill({ selector, text, value, index = 0 }) {
    if (value === undefined || value === null) throw new Error('value 必填');
    const hit = await locate({ selector, text });
    if (!hit) throw new Error('没找到输入框: ' + (selector || text));
    const target = index > 0 ? hit.frame.locator(selector).nth(index) : hit.loc;
    await target.fill(String(value), { timeout: 15000 });
    return { filled: true, into: selector || text };
  },

  async click({ selector, text, timeout = 15000 }) {
    const hit = await locate({ selector, text });
    if (!hit) throw new Error('没找到可点元素: ' + (selector || text));
    try {
      await hit.loc.click({ timeout });
    } catch (e) {
      // 文本命中的可能是 span，往上找真正的按钮/链接
      const bumped = hit.loc.locator('xpath=ancestor-or-self::*[self::button or self::a or @role="button"][1]');
      if (await bumped.count().catch(() => 0)) await bumped.first().click({ timeout });
      else throw e;
    }
    await (await ensure()).waitForTimeout(800);
    return { clicked: selector || text };
  },

  async press({ key = 'Enter' }) {
    const p = await ensure();
    await p.keyboard.press(key);
    return { pressed: key };
  },

  async texts({ max = 1200 }) {
    const p = await ensure();
    const out = [];
    for (const f of p.frames()) {
      let t = '';
      try {
        t = (await f.evaluate(() => document.body && document.body.innerText)) || '';
      } catch (e) {
        t = '[读取失败] ' + String(e.message).slice(0, 80);
      }
      out.push({ url: f.url().slice(0, 110), text: t.replace(/\n{2,}/g, '\n').slice(0, max) });
    }
    return { frames: out };
  },

  async text({ selector, max = 3000 }) {
    const p = await ensure();
    if (!selector) return { text: (await p.evaluate(() => document.body.innerText)).slice(0, max) };
    const hit = await locate({ selector });
    if (!hit) throw new Error('没找到元素: ' + selector);
    return { text: (await hit.loc.innerText()).slice(0, max) };
  },

  // 逃生舱：页面里跑任意 JS（找入口、读接口数据、点隐藏按钮）
  async eval({ script, frame_url }) {
    const p = await ensure();
    let target = p.mainFrame();
    if (frame_url) {
      const f = p.frames().find((x) => x.url().includes(frame_url));
      if (!f) throw new Error('没找到 frame: ' + frame_url);
      target = f;
    }
    const out = await target.evaluate(script);
    return { result: typeof out === 'string' ? out.slice(0, 4000) : out };
  },

  async shot({ full = false, name }) {
    const p = await ensure();
    const file = `${ROOT}/${name || 'shot-' + Date.now()}.png`;
    await p.screenshot({ path: file, fullPage: !!full });
    return { path: file, bytes: fs.statSync(file).size, url: p.url() };
  },

  async close() {
    if (ctx) await ctx.close();
    ctx = null;
    page = null;
    return { closed: true };
  },
};

http
  .createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', async () => {
      let args = {};
      try {
        args = body ? JSON.parse(body) : {};
      } catch (_) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ ok: false, error: 'body 不是合法 JSON' }));
      }
      const action = args.action || 'status';
      const fn = CMDS[action];
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      if (!fn) {
        res.writeHead(404);
        return res.end(JSON.stringify({ ok: false, error: '未知 action: ' + action, actions: Object.keys(CMDS) }));
      }
      try {
        const out = (await fn(args)) || {};
        res.writeHead(200);
        res.end(JSON.stringify({ ok: true, action, ...out }, null, 1));
      } catch (e) {
        res.writeHead(500);
        res.end(JSON.stringify({ ok: false, action, error: String((e && e.message) || e).slice(0, 700) }, null, 1));
      }
    });
  })
  .listen(PORT, '127.0.0.1', () => log(`browser daemon on 127.0.0.1:${PORT}, profile=${PROFILE}`));
