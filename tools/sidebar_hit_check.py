# -*- coding: utf-8 -*-
"""侧边栏（#stage-tools）在全屏聊天下的可点性实测。

只回答一个问题：全屏聊天时，侧边栏按钮的屏幕中心点上，elementFromPoint
返回的是谁？如果是 #chat-panel 或它的子元素，就是被盖住了。

用法：python tools/sidebar_hit_check.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "http://localhost:8001/"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
() => {
  const out = {};
  const tools = document.getElementById('stage-tools');
  const panel = document.getElementById('chat-panel');
  const toggle = document.getElementById('stage-tools-toggle');
  if (!tools || !panel) return { error: 'missing #stage-tools or #chat-panel' };

  const cs = (el) => {
    const s = getComputedStyle(el);
    return { z: s.zIndex, pos: s.position, pe: s.pointerEvents, disp: s.display };
  };
  out.toolsStyle = cs(tools);
  out.panelStyle = cs(panel);
  out.panelClasses = panel.className;
  out.toolsClasses = tools.className;
  out.htmlClasses = document.documentElement.className;

  // 侧边栏的父节点链（确认它到底挂在哪）
  const chain = [];
  let n = tools.parentElement;
  while (n && chain.length < 6) { chain.push(n.id || n.tagName); n = n.parentElement; }
  out.parentChain = chain;

  // 命中测试：拿 toggle 按钮（收缩态唯一可见的）中心点问浏览器
  const target = toggle || tools;
  const r = target.getBoundingClientRect();
  out.toggleRect = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  const cx = Math.round(r.x + r.width / 2);
  const cy = Math.round(r.y + r.height / 2);
  out.probePoint = { x: cx, y: cy };
  const hit = document.elementFromPoint(cx, cy);
  out.hitId = hit ? (hit.id || hit.className || hit.tagName) : null;
  out.hitIsInsideTools = hit ? tools.contains(hit) : false;
  return out;
}
"""


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright，先装：venv/bin/pip install playwright")
        return 2

    with sync_playwright() as p:
        # 本机 Playwright 版本与缓存里的 chromium 版本号对不上（要 1243、只有 1208），
        # 直接指到已装好的可执行文件，避免为了跑一次命中测试去下载整个浏览器。
        exe = os.path.expanduser("~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome")
        browser = p.chromium.launch(headless=True, executable_path=exe,
                                    args=["--ignore-certificate-errors"])
        page = browser.new_page(viewport={"width": 1280, "height": 800},
                                ignore_https_errors=True)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        print("=== 1) 默认（半屏）状态 ===")
        print(page.evaluate(PROBE))

        print("\n=== 2) 进全屏聊天后 ===")
        page.evaluate("() => { if (window.App && App.setChatFullscreen) App.setChatFullscreen(true); }")
        page.wait_for_timeout(900)
        print(page.evaluate(PROBE))

        print("\n=== 3) 全屏透明后 ===")
        page.evaluate("() => { if (window.App && App.setChatGhost) App.setChatGhost(true); }")
        page.wait_for_timeout(900)
        print(page.evaluate(PROBE))

        # 4) 真点击：命中测试说「在上面」不等于「点得动」——
        #    过渡动画、pointer-events、事件被别处吞掉都会让 click 落空。
        print("\n=== 4) 真实点击 toggle 按钮 ===")
        page.evaluate("() => { if (window.App && App.setChatFullscreen) App.setChatFullscreen(true); }")
        page.wait_for_timeout(900)
        before = page.evaluate("() => document.getElementById('stage-tools').className")
        try:
            page.click("#stage-tools-toggle", timeout=3000)
            clicked = "ok"
        except Exception as e:
            clicked = "FAIL: %s" % str(e).split("\n")[0]
        page.wait_for_timeout(500)
        after = page.evaluate("() => document.getElementById('stage-tools').className")
        print("click=%s" % clicked)
        print("before=%r  after=%r  changed=%s" % (before, after, before != after))

        # 5) 展开态：全屏下逐个按钮命中测试 —— 「toggle 能点」不等于「所有按钮都能点」。
        #    轮盘外的按钮被 overflow:hidden 裁掉，属于设计上不可见，不计入被盖住。
        print("\n=== 5) 全屏展开态：逐个按钮命中 ===")
        page.evaluate("() => { if (window.App && App.setChatFullscreen) App.setChatFullscreen(true); }")
        page.wait_for_timeout(600)
        if "collapsed" in page.evaluate("() => document.getElementById('stage-tools').className"):
            page.click("#stage-tools-toggle", timeout=3000)
            page.wait_for_timeout(700)
        rows = page.evaluate(r"""
        () => {
          const tools = document.getElementById('stage-tools');
          const ring = document.getElementById('stage-tools-ring');
          const rr = ring ? ring.getBoundingClientRect() : null;
          return [...tools.querySelectorAll('button')].map(b => {
            const r = b.getBoundingClientRect();
            if (r.width === 0 || r.height === 0 || getComputedStyle(b).display === 'none') {
              return { id: b.id, visible: false, why: 'hidden' };
            }
            const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
            const inRing = !rr || (cx >= rr.left && cx <= rr.right && cy >= rr.top && cy <= rr.bottom);
            const hit = document.elementFromPoint(cx, cy);
            return { id: b.id, visible: inRing, why: inRing ? '' : 'wheel-off',
                     at: [cx, cy], self: !!hit && (hit === b || b.contains(hit)),
                     hit: hit ? (hit.id || hit.className || hit.tagName) : null };
          });
        }
        """)
        for r in rows:
            print("%-20s visible=%-5s self=%-5s hit=%s %s" % (
                r.get("id") or "(no-id)", r.get("visible"), r.get("self"), r.get("hit"), r.get("why") or ""))
        checked = [r for r in rows if r.get("visible")]
        covered = [r for r in checked if not r.get("self")]
        print("轮盘内可见按钮 %d 个，被盖住 %d 个" % (len(checked), len(covered)))

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
