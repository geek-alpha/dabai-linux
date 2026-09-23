# -*- coding: utf-8 -*-
"""内联工具块折叠行为 · 真浏览器实测（真 CSS + 服务器转译的真 JS）。

为什么不能只用桩 DOM：折叠是 CSS 与 JS 的契约，桩测只能证明 JS 切了类，
证明不了「类变了，块真的收起来/摊开」。这里量 clientHeight 和 computed
grid-template-rows，看到的是浏览器算出来的真实几何。

用法：CAST_CHECK_EXECUTABLE=<chrome> venv/bin/python tools/tool_fold_check.py
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "http://127.0.0.1:8001/"
EXE = os.environ.get("CAST_CHECK_EXECUTABLE") or "/opt/ms-playwright/chromium-1208/chrome-linux64/chrome"
ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append("%s%s" % (name, ("  ← " + detail) if detail else ""))


# 一次 evaluate 内完成「建块 + 读数」：页面是活的，中间不 await 才不会串进真实事件
PROBE = r"""
() => {
  const host = document.getElementById('messages') || document.querySelector('.messages');
  if (!host) return { err: 'no .messages container' };
  host.innerHTML = '';
  _App._turnMsgEl = null;
  _App.toolChainBeginTurn();
  _App.toolChainStart('probe_tool', { path: '/tmp/demo.txt', mode: 'r' });
  const d = host.querySelector('.tool-inline');
  if (!d) return { err: 'block not created' };
  const body = d.querySelector('.tool-inline-body');
  const cs = getComputedStyle(body);
  return {
    cls: d.className,
    hasOpen: d.classList.contains('open'),
    bodyHidden: body.hidden,
    rows: cs.gridTemplateRows,
    display: cs.display,
    h: body.clientHeight,
    headH: d.querySelector('.tool-inline-head').clientHeight,
  };
}
"""

CLICK = r"""
() => {
  const host = document.querySelector('.messages');
  const d = host.querySelector('.tool-inline');
  const body = d.querySelector('.tool-inline-body');
  const before = { open: d.classList.contains('open'), h: body.clientHeight };
  d.querySelector('.tool-inline-head').click();
  const cs = getComputedStyle(body);
  return {
    before,
    after: { open: d.classList.contains('open'), hidden: body.hidden, rows: cs.gridTemplateRows, h: body.clientHeight },
  };
}
"""

READ = r"""
() => {
  const host = document.querySelector('.messages');
  const d = host.querySelector('.tool-inline');
  if (!d) return { err: 'block gone' };
  const body = d.querySelector('.tool-inline-body');
  return {
    cls: d.className,
    open: d.classList.contains('open'),
    hidden: body.hidden,
    display: getComputedStyle(body).display,
    rows: getComputedStyle(body).gridTemplateRows,
    h: body.clientHeight,
    caret: (d.querySelector('.tool-inline-caret') || {}).textContent,
    aria: (d.querySelector('.tool-inline-head') || {}).getAttribute('aria-expanded'),
  };
}
"""


async def legacy_css_round(p):
    """旧 CSS + 新 JS：验证 hidden 兼容位真的能让块收起来。

    旧 CSS（v1.1.20 之前）的 .tool-inline-body 只是个普通块，折叠全靠 hidden
    属性的 UA 样式。新 JS 如果只切 open 类，在旧 CSS 下就是「怎么点都不动」。
    """
    import subprocess
    old_css = subprocess.check_output(
        ["git", "-C", "/home/wxf/dabai", "show", "HEAD:web/style.css"]).decode("utf-8")
    if ".tool-inline:not(.open)" in old_css:
        check("HEAD 版 CSS 确实是旧折叠契约（无 :not(.open) 规则）", False,
              "拿到的旧 CSS 里已有新规则，这轮兼容测试失去意义")
        return
    b = await p.chromium.launch(executable_path=EXE)
    pg = await b.new_page(viewport={"width": 1280, "height": 900})
    await pg.route("**/ws**", lambda r: asyncio.ensure_future(r.abort()))
    await pg.route("**/static/style.css*",
                   lambda r: asyncio.ensure_future(r.fulfill(status=200, content_type="text/css", body=old_css)))
    await pg.goto(URL, wait_until="domcontentloaded")
    await pg.wait_for_timeout(6000)
    r = await pg.evaluate(PROBE)
    print("⑦ 旧 CSS 下的新建块", r)
    check("旧 CSS 下新建块也是展开的（几何 > 0）", r.get("h", 0) > 0, str(r))
    await pg.evaluate(CLICK)
    await pg.wait_for_timeout(450)
    g = await pg.evaluate(READ)
    print("⑦ 旧 CSS 下点击后", g)
    check("旧 CSS 下点击能真的收起来（hidden 兼容位生效）", g["h"] == 0,
          "clientHeight=%s hidden=%s" % (g["h"], g.get("hidden")))
    await pg.evaluate(CLICK)
    await pg.wait_for_timeout(450)
    g2 = await pg.evaluate(READ)
    check("旧 CSS 下再点一下能展开回来", g2["h"] > 0, str(g2))
    await b.close()


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=EXE)
        pg = await b.new_page(viewport={"width": 1280, "height": 900})
        # 掐断实时通道：不让大白的真实工具调用插进这次测量
        await pg.route("**/ws**", lambda r: asyncio.ensure_future(r.abort()))
        await pg.goto(URL, wait_until="domcontentloaded")
        await pg.wait_for_timeout(6000)

        r = await pg.evaluate(PROBE)
        if r.get("err"):
            print("✘ 探针失败：", r["err"])
            await b.close()
            return 1
        print("① 新建块（运行中）", r)
        check("运行中块默认展开（有 open 类）", r["hasOpen"], r["cls"])
        check("运行中详情真的可见（高 > 0）", r["h"] > 0, "clientHeight=%s rows=%s" % (r["h"], r["rows"]))
        check("CSS 用的是 grid 折叠契约", r["display"] == "grid", "display=%s" % r["display"])

        c = await pg.evaluate(CLICK)
        await pg.wait_for_timeout(450)
        geo = await pg.evaluate(READ)
        print("② 点击头部后 0.45s", geo)
        check("点击后 open 类翻转", c["before"]["open"] != c["after"]["open"],
              "%s → %s" % (c["before"]["open"], c["after"]["open"]))
        check("点击后几何真的收起（归零，不留空档）", geo["h"] == 0,
              "clientHeight=%s rows=%s" % (geo["h"], geo["rows"]))

        await pg.wait_for_timeout(2200)
        a = await pg.evaluate(READ)
        print("③ 再等 2.2s（手动点过，应保持用户选择）", a)
        check("手动点过的块不被自动折叠翻回去", a["open"] == c["after"]["open"],
              "期望 open=%s，实际 %s" % (c["after"]["open"], a["open"]))

        # 再来一块，全程不点：验证运行中展开 → 完成 2 秒自动折叠
        r2 = await pg.evaluate(PROBE)
        await pg.wait_for_timeout(300)
        await pg.evaluate("() => _App.toolChainResult('probe_tool', 'ok\\nline2', true)")
        just_done = await pg.evaluate(READ)
        print("④ 刚完成", just_done)
        check("完成后仍展开（留给用户瞄一眼）", just_done["open"], str(just_done))
        await pg.wait_for_timeout(2600)
        folded = await pg.evaluate(READ)
        print("⑤ 完成后 2.6 秒", folded)
        check("完成后自动折叠", not folded["open"], str(folded))
        check("折叠后几何归零", folded["h"] == 0, "clientHeight=%s rows=%s" % (folded["h"], folded["rows"]))
        check("折叠后箭头朝右", folded["caret"] == "▸", str(folded["caret"]))
        check("折叠后 aria-expanded=false", folded["aria"] == "false", str(folded["aria"]))

        # 历史恢复：重放出来的块（都已结束）应当直接收起
        await pg.evaluate(PROBE)
        await pg.wait_for_timeout(300)
        await pg.evaluate("() => _App.toolChainResult('probe_tool', 'r', true)")
        await pg.wait_for_timeout(100)
        pre = await pg.evaluate(READ)
        await pg.evaluate("() => _App.toolChainCollapseAll()")
        await pg.wait_for_timeout(450)
        post = await pg.evaluate(READ)
        print("⑥ 历史重放收起", post)
        check("collapseAll 前是展开的", pre["open"], str(pre))
        check("collapseAll 后收起归零", (not post["open"]) and post["h"] == 0, str(post))

        # ⑦ 旧 JS 契约：不走产品 JS，直接按旧版方式切 hidden，
        # 新 CSS 必须认——浏览器缓存半新半旧（JS 旧 / CSS 新）时的退路。
        await pg.evaluate("""() => {
          const d = document.querySelector('.messages .tool-inline');
          const body = d.querySelector('.tool-inline-body');
          d.classList.add('open');
          body.hidden = false;
        }""")
        await pg.wait_for_timeout(450)
        g4 = await pg.evaluate(READ)
        check("旧 JS 展开（hidden=false）在新 CSS 下真能展开", g4["h"] > 0, str(g4))
        await pg.evaluate("""() => {
          const d = document.querySelector('.messages .tool-inline');
          const body = d.querySelector('.tool-inline-body');
          d.classList.add('open');
          body.hidden = true;
        }""")
        await pg.wait_for_timeout(450)
        g3 = await pg.evaluate(READ)
        print("⑦ 旧 JS 只切 hidden", g3)
        check("旧 JS 收起（hidden=true）在新 CSS 下真能收起", g3["h"] == 0, str(g3))

        # ⑧ 旧 CSS + 新 JS：浏览器缓存半新半旧时的真实处境。
        # 旧 CSS 里没有 .tool-inline:not(.open) 规则，折叠只认 hidden 属性 ——
        # 这曾经就是「点击没反应、也不自动折叠」的现场。
        await b.close()
        await legacy_css_round(p)

    print()
    for x in ok:
        print("PASS", x)
    for x in bad:
        print("FAIL", x)
    print("\n%d/%d 通过" % (len(ok), len(ok) + len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
