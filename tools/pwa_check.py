#!/usr/bin/env python3
"""PWA 链路实测：Service Worker 注册 + 二次加载的流量对比。

为什么必须真跑浏览器：SW 注册依赖「安全上下文 + 证书被信任」两个条件，
curl 能看到 200 但看不出 SW 是否注册成功；而这条链路断了的表现是
「页面能开、但每次还是全量重下」，从服务端日志上完全看不出来。

用法：./venv/bin/python tools/pwa_check.py [--base https://127.0.0.1:8000]
"""
import argparse
import json
import os
import sys

from playwright.sync_api import sync_playwright


def wire_bytes(ctx, page):
    """收集真实上网字节数（encodedDataLength，含响应头，不含磁盘缓存命中）。"""
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    stats = {"total": 0, "urls": [], "from_disk": 0}

    def on_finished(p):
        nonlocal stats
        stats["total"] += p.get("encodedDataLength", 0)
        stats["urls"].append(p.get("requestId"))

    def on_request(p):
        # servedFromCache 有值 = 浏览器磁盘缓存命中，零网络流量
        if p.get("response", {}).get("fromDiskCache") or p.get("response", {}).get("fromServiceWorker"):
            stats["from_disk"] += 1

    cdp.on("Network.loadingFinished", on_finished)
    cdp.on("Network.requestWillBeSent", on_request)
    return stats, cdp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8000")
    ap.add_argument("--fetch-model", action="store_true",
                    help="额外验证 24MB VRM 能否进 SW 缓存（第二次打开秒开的前提）")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    out = {}

    with sync_playwright() as p:
        # 容器/系统里通常只有发行版 chromium，没有 playwright 自带的 headless_shell
        exe = next((c for c in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                                "/usr/bin/google-chrome") if os.path.exists(c)), None)
        browser = p.chromium.launch(
            executable_path=exe,
            args=["--ignore-certificate-errors", "--no-sandbox",
                  "--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 412, "height": 915})
        page = ctx.new_page()
        page.set_default_timeout(45000)

        # --- 1. SW 注册探针（打真实首页）---
        # 别用 /_swprobe.html：server.py 只给 /sw.js 和 /manifest.webmanifest 单独
        # 注册了根路径路由，探针页在根路径下是 404，等 #r 只会等成超时。
        # 首页自己会注册 SW，这里再显式注册一次是为了把失败原因 catch 出来。
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        out["probe"] = page.evaluate("""async () => {
            const info = { secureContext: window.isSecureContext,
                           swSupported: 'serviceWorker' in navigator };
            try {
                const r = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
                info.registered = r.scope;
                info.active = !!r.active;
            } catch (e) { info.error = e.name + ': ' + e.message; }
            return info;
        }""")

        # --- 2. 首次加载：全新 context（无 HTTP 磁盘缓存、无 SW）---
        # 探针那个 context 已经访问过首页，磁盘缓存里已经有东西，
        # 拿它测首访会偏低；而且 about:blank 不是安全上下文，
        # caches API 在那里直接 ReferenceError。
        ctx.close()
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 412, "height": 915})
        page = ctx.new_page()
        page.set_default_timeout(45000)
        stats1, cdp1 = wire_bytes(ctx, page)
        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(8000)
        out["first_load"] = {"bytes": stats1["total"], "requests": len(stats1["urls"])}

        # --- 3. 二次加载：同一 context，量缓存收益 ---
        stats2, cdp2 = wire_bytes(ctx, page)
        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(8000)
        out["second_load"] = {"bytes": stats2["total"], "requests": len(stats2["urls"])}

        # --- 4. SW 状态与缓存条目 ---
        out["sw"] = page.evaluate("""async () => {
            const r = await navigator.serviceWorker.getRegistration();
            const keys = await caches.keys();
            let entries = 0;
            for (const k of keys) { const c = await caches.open(k);
                entries += (await c.keys()).length; }
            return { scope: r && r.scope, active: !!(r && r.active),
                     controller: !!navigator.serviceWorker.controller,
                     caches: keys, entries };
        }""")
        # --- 5. 大模型入缓存探针：24MB 的 VRM 是手机端慢的根源 ---
        # 单条缓存上限必须大于模型体积，且策略要是 cache-first；
        # 只看 SW 是否 active 会漏掉这个——SW 活着但模型进不去，照样每次重下 24MB。
        if args.fetch_model:
            out["model"] = page.evaluate("""async () => {
                const url = '/models/' + encodeURIComponent('白头凤.vrm');
                const t0 = performance.now();
                await (await fetch(url)).arrayBuffer();
                const t1 = performance.now();
                await (await fetch(url)).arrayBuffer();
                const t2 = performance.now();
                await new Promise((res) => setTimeout(res, 6000));
                const keys = await caches.keys();
                let cached = false, entries = 0;
                for (const k of keys) {
                    const c = await caches.open(k);
                    const reqs = await c.keys();
                    entries += reqs.length;
                    if (reqs.some((r) => r.url.includes('/models/'))) cached = true;
                }
                return { first_ms: Math.round(t1 - t0), second_ms: Math.round(t2 - t1),
                         cached, entries };
            }""")

        browser.close()

    f, s = out["first_load"], out["second_load"]
    out["saving_pct"] = round((1 - s["bytes"] / f["bytes"]) * 100, 1) if f["bytes"] else 0
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
