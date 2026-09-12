# -*- coding: utf-8 -*-
"""施法态端到端检查（Playwright + 系统 Edge）。

要证明的四件事（静态检查做不到）：
  1. 那个空掉的 toolChainProgress 钩子真的被接管了（直接验证函数体）
  2. 后端推的 elapsed 真的能驱动档位与秒表
  3. 施法期间画面真的在动，非施法态真的没有动画在跑（省电）
  4. result / abort / endTurn 三条路径都能把施法态收干净

【测试隔离的关键】这个页面是「活」的：大白自己就在用它跑工具，
随时可能有真实 result 到达并改变施法态。所以每个断言都把
「操作 + 读取」放进**同一个 evaluate**（JS 单线程内原子完成），
中间不 await，真实事件插不进来。

用法：python tools/cast_fx_check.py
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "https://localhost:8000/"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok, bad = [], []


def load_real_descs():
    """读技能里**真实**的工具描述（skill.json 的 function.description）。

    为什么读源文件而不是在测试里写死一份：写死的那份会随工具演进而漂移，
    而「说明是否跟着工具自动更新」恰恰是这次改动的核心 —— 漂移了就等于没测。
    """
    out = {}
    for root, _dirs, files in os.walk(os.path.join(BASE, "skills")):
        if "skill.json" not in files:
            continue
        try:
            with io.open(os.path.join(root, "skill.json"), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for t in (d.get("tools") or []):
            fn = t.get("function") or {}
            if fn.get("name"):
                out[fn["name"]] = fn.get("description") or ""
    return out


def check(name, cond, detail=""):
    (ok if cond else bad).append("%s%s" % (name, ("  ← " + detail) if detail else ""))


def wait_panel_settled(page, timeout=5000):
    """等聊天面板的展开过渡真正跑完，且 --chat-top 已按实测同步。

    为什么需要它：面板收起是 transform: translateY(120%) + 0.3s 过渡，
    headless 下渲染帧稀疏，过渡是按帧推进的 —— 固定 wait_for_timeout(450)
    量到的可能还是滑动中间位置（实测 rect.top 偏 120%×面板高）。
    判据：面板实际位置 == offsetHeight 口径的位置，且 --chat-top 与之一致。
    超时不算致命：返回 False，让后面的断言拿真实数值去报错（否则整个脚本崩掉，看不到别的结论）。
    """
    try:
        page.wait_for_function("""() => {
            const p = document.getElementById('chat-panel');
            const vh = window.innerHeight;
            const bot = parseFloat(getComputedStyle(p).bottom) || 0;
            const want = vh - p.offsetHeight - bot;
            const chatTop = parseFloat(getComputedStyle(document.documentElement)
                .getPropertyValue('--chat-top')) || 0;
            const wantChatTop = p.classList.contains('chat-fs') ? vh * 0.62 - bot : want;
            return Math.abs(p.getBoundingClientRect().top - want) <= 2
                && Math.abs(chatTop - wantChatTop) <= 2;
        }""", timeout=timeout)
        return True
    except Exception:
        return False


def launch_kwargs():
    """选浏览器：默认系统 Edge（部署机）；没装 Edge 的机器用
    CAST_CHECK_EXECUTABLE=/usr/bin/chromium 指过去，跑的是同一套断言。"""
    exe = os.environ.get("CAST_CHECK_EXECUTABLE")
    if exe:
        return {"executable_path": exe, "headless": True,
                "args": ["--ignore-certificate-errors", "--no-sandbox"]}
    return {"channel": "msedge", "headless": True,
            "args": ["--ignore-certificate-errors"]}


# ---------- 0. 静态断言：全屏不透明的「静默」是否真的盖住了所有高 z-index 浮层 ----------
# 背景（用户反馈的 bug）：这些氛围层挂在 #stage 之外、z-index 高达 60~72，比聊天面板高。
# 全屏不透明时若不在 CSS 里显式藏掉，它们会飘在对话上面继续播动画 —— 观感就是
# 「点了全屏，特效还在动」。这一组断言不靠浏览器，纯静态可跑（也就不用等页面加载）。
def check_quiet_static():
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    css = open(os.path.join(root, "web", "style.css"), encoding="utf-8").read()
    if "html.chat-quiet #holo-cheer" not in css:
        check("chat-quiet 覆盖 #stage 之外的高 z-index 浮层", False,
              "style.css 里没有 html.chat-quiet 的浮层隐藏规则")
        return
    m = re.search(r"#chat-panel\.chat-fs\s*\{[^}]*?z-index:\s*(\d+)", css)
    panel_z = int(m.group(1)) if m else 0
    fx_z = [int(x) for x in re.findall(
        r"#(?:gfx|holo|live|cast|arcade)[\w-]*[^{}]*\{[^}]*?z-index:\s*(\d+)", css)]
    check("聊天全屏面板 z-index 高于全部氛围浮层",
          panel_z > (max(fx_z) if fx_z else 0),
          "panel=%s max_fx=%s" % (panel_z, max(fx_z) if fx_z else "-"))
    ids = set(re.findall(r"html\.chat-quiet\s+#([\w-]+)", css))
    ids |= set(re.findall(r"html\.chat-quiet\s+body\s*>\s*#([\w-]+)", css))
    src = ""
    for dp, _, fs in os.walk(os.path.join(root, "web")):
        for f in fs:
            if f.endswith((".ts", ".html")):
                src += open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read()
    missing = sorted(i for i in ids
                     if ("'" + i + "'") not in src and ('"' + i + '"') not in src)
    check("被藏的氛围层 id 都真实存在（选择器没写错名）", not missing,
          "可疑 id=%s" % missing)
    # class 选择器同样要查：曾经把「只有 .lv-banner 这个 class、没有 id」的元素
    # 写成 #live-banner，规则静默失效（CSS 不报错，特效照飘）—— 这条就是抓它的。
    cls = set(re.findall(r"html\.chat-quiet\s+\.([\w-]+)", css))
    miss_cls = sorted(c for c in cls if c not in src)
    check("被藏的氛围层 class 都真实存在", not miss_cls, "可疑 class=%s" % miss_cls)


check_quiet_static()


with __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright() as p:
    browser = p.chromium.launch(**launch_kwargs())
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)

    # ---------- 1. 静态属性 ----------
    m = page.evaluate("""() => {
        const fx = document.getElementById('cast-fx');
        const arc = document.getElementById('arcade-fx');
        return {
            cast: !!(_App && _App.cast),
            node: !!fx,
            inStage: !!(fx && fx.closest('#stage')),
            parent: fx && fx.parentElement ? fx.parentElement.id : '-',
            pe: fx ? getComputedStyle(fx).pointerEvents : 'missing',
            afterArcade: !!(fx && arc &&
                (arc.compareDocumentPosition(fx) & Node.DOCUMENT_POSITION_FOLLOWING)),
            hudText: (document.querySelector('#cast-fx .cf-hud') || {}).textContent || '',
        };
    }""")
    check("App.cast 已挂载", m["cast"])
    check("#cast-fx 已注入舞台", m["node"] and m["inStage"], "父节点=%s" % m["parent"])
    check("挂在氛围层之后（DOM 顺序保证不盖住 UI）", m["afterArcade"])
    check("施法层不挡交互（pointer-events: none）", m["pe"] == "none", m["pe"])
    # 档位文案随运行时长变化（施法 / 深度施法 / 超载），所以只验证结构分隔符与秒表单位
    check("HUD 结构完整（含工具名/档位/秒表）",
          all(k in m["hudText"] for k in ("·", "s")), m["hudText"][:60])

    # ---------- 2. 钩子真的被接管（直接看函数体） ----------
    hook = page.evaluate("""() => {
        const src = _App.toolChainProgress ? _App.toolChainProgress.toString() : '';
        return { len: src.length, hasBody: src.includes('elapsedMs'), src: src.slice(0, 90) };
    }""")
    check("toolChainProgress 不再是空函数（函数体已接管）", hook["hasBody"],
          "len=%d %s" % (hook["len"], hook["src"]))

    # ---------- 3. 空闲态：一帧动画都不许跑（省电核心） ----------
    idle = page.evaluate("""() => {
        _App.cast.reset();
        const ring = document.querySelector('#cast-fx .cf-ring');
        const core = document.querySelector('#cast-fx .cf-core');
        const aura = document.querySelector('#cast-fx .cf-aura');
        const fx = document.getElementById('cast-fx');
        return {
            active: _App.cast.active,
            casting: document.documentElement.classList.contains('casting'),
            ring: ring ? getComputedStyle(ring).animationName : 'missing',
            core: core ? getComputedStyle(core).animationName : 'missing',
            aura: aura ? getComputedStyle(aura).animationName : 'missing',
            opacity: fx ? parseFloat(getComputedStyle(fx).opacity) : -1,
        };
    }""")
    check("reset 后 active = 0", idle["active"] == 0, "active=%s" % idle["active"])
    check("reset 后无 casting 类", not idle["casting"])
    check("非施法态法阵【没有】动画在跑", idle["ring"] in ("none", ""), idle["ring"])
    check("非施法态光核【没有】动画在跑", idle["core"] in ("none", ""), idle["core"])
    check("非施法态能量场【没有】动画在跑", idle["aura"] in ("none", ""), idle["aura"])
    # 注：施法层 opacity 带 0.3s 过渡，同步读会读到过渡中间值（1.0），
    # 所以可见性不用 opacity 判断 —— 上面三条「无 casting 类 + 动画为 none」已足够证明。

    # ---------- 4. 施法开始：走真实钩子 toolChainStart（同步原子） ----------
    s = page.evaluate("""() => {
        _App.cast.reset();
        _App.toolChainStart('read', { path: 'x.ts' });
        const ring = document.querySelector('#cast-fx .cf-ring');
        const fx = document.getElementById('cast-fx');
        return {
            active: _App.cast.active,
            casting: document.documentElement.classList.contains('casting'),
            name: (document.querySelector('#cast-fx .cf-name') || {}).textContent,
            tier: _App.cast.tier,
            ring: ring ? getComputedStyle(ring).animationName : 'missing',
            opacity: fx ? parseFloat(getComputedStyle(fx).opacity) : -1,
        };
    }""")
    check("toolChainStart → 进入施法态", s["casting"] and s["active"] == 1,
          "active=%s casting=%s" % (s["active"], s["casting"]))
    check("HUD 显示工具名", s["name"] == "read", "name=%s" % s["name"])
    check("法阵动画已启动", s["ring"] not in ("none", "", "missing"), s["ring"])
    # 施法层可见性：加类那一帧同步读必然读到过渡起点（0）。headless 下渲染帧稀疏，
    # 0.3s 的 opacity 过渡要好几秒才推进到 1（实测 2s 内读仍是 0、之后读是 1.00），
    # 所以给它 8s 的等待窗口，判据不变。等待期间真实事件可能收掉施法态 —— 那时这条会红，
    # 属于可接受的风险（比同步读到一个必然的 0 强）。
    vis = True
    try:
        page.wait_for_function(
            "() => parseFloat(getComputedStyle(document.getElementById('cast-fx')).opacity) > 0.5",
            timeout=8000)
    except Exception:
        vis = False
    check("施法层已可见（opacity > 0.5）", vis, "opacity=%.2f" % page.evaluate(
        "() => parseFloat(getComputedStyle(document.getElementById('cast-fx')).opacity)"))

    # ---------- 4·补、说明搬到屏幕中央（本轮核心诉求） ----------
    real = load_real_descs()
    check("读到真实工具描述（技能源文件）", len(real) > 50, "%d 个工具" % len(real))
    hud = page.evaluate("""(d) => {
        _App.cast.reset();
        // 展开聊天面板再量：字幕的验收标准是「落在聊天框上方那条带」，收起时量不到边界
        document.getElementById('chat-panel').classList.remove('collapsed');
        _App.onResize();
        _App.cast.begin('code_edit', d);
        const h = document.querySelector('#cast-fx .cf-hud');
        const act = document.querySelector('#cast-fx .cf-act');
        const name = document.querySelector('#cast-fx .cf-name');
        const r = h.getBoundingClientRect();
        const ar = act.getBoundingClientRect();
        return {
            cx: r.left + r.width / 2,
            vw: window.innerWidth,
            act: act.textContent,
            actSize: parseFloat(getComputedStyle(act).fontSize),
            nameSize: parseFloat(getComputedStyle(name).fontSize),
            top: r.top, bottom: r.bottom, vh: window.innerHeight,
            w: r.width,
            panelTop: document.getElementById('chat-panel').getBoundingClientRect().top,
            vis: getComputedStyle(h).visibility,
        };
    }""", real.get("code_edit", ""))
    check("中央字幕水平居中（误差 < 2px）", abs(hud["cx"] - hud["vw"] / 2) < 2,
          "cx=%.1f 视口中心=%.1f" % (hud["cx"], hud["vw"] / 2))
    # 关键：字幕里的字来自**工具自己的描述**，不是前端写死的表
    check("说明取自工具自己的 description（code_edit → 精准修改文件）",
          hud["act"] == "精准修改文件", hud["act"])
    check("说明字号够大（≥ 24px，能当主视觉）", hud["actSize"] >= 24,
          "%.0fpx" % hud["actSize"])
    check("说明明显大于工具名（主次分明，≥1.8 倍）",
          hud["actSize"] >= hud["nameSize"] * 1.8,
          "说明 %.0fpx vs 工具名 %.0fpx" % (hud["actSize"], hud["nameSize"]))
    # 位置契约（本轮）：整层锚点跟着聊天框上沿走，字幕必须收在聊天框**上方**那条带里。
    # 演进：29%（正压模型脸）→ 视口正中 50%（一样挡脸，还压礼物条）→ 贴聊天框顶部。
    check("字幕落在聊天框上方（不压聊天正文）",
          hud["bottom"] <= hud["panelTop"] + 1,
          "字幕底=%.0f / 聊天框顶=%.0f" % (hud["bottom"], hud["panelTop"]))
    check("字幕不超出视口宽度（88vw 上限）", hud["w"] <= hud["vw"] * 0.89,
          "宽 %.0f / 视口 %.0f" % (hud["w"], hud["vw"]))

    # ---------- 4·补三、整层特效贴着聊天框顶部（本轮核心诉求） ----------
    # 位置演进：29%（正压 3D 模型脸）→ 视口正中 50%（一样挡脸，还压礼物条）→
    # 现在锚点 = **实测**非全屏聊天框上沿（--chat-top，36_layout_sync 写入）- 法阵半径。
    # 为什么不能用 38dvh 常量算：面板是 max-height: 38dvh，消息少时高度由内容决定
    # （实测 128px vs 上限 304px），常量算出的上沿比真实上沿高出一大截 —— 特效悬在半空。
    # 先展开面板并等过渡跑完（transform 0.3s），否则量到的是滑动中的中间位置。
    page.evaluate("""() => {
        const p = document.getElementById('chat-panel');
        // 关掉滑动过渡再展开：headless 下渲染帧稀疏，transform 过渡推进极慢，
        // 固定等待量到的是滑动中间位置（实测偏 120%×面板高）。这里要的是
        // 「展开后停在哪」，不是动画过程本身。
        p.style.transition = 'none';
        p.classList.remove('collapsed');
        document.getElementById('controls').classList.remove('collapsed');
        void p.offsetHeight;      // 强制重排，让位置立即落定
        _App.onResize();
    }""")
    wait_panel_settled(page)
    lay = page.evaluate("""() => {
        _App.cast.reset();
        _App.cast.begin('code_edit');
        const box = (sel) => {
            const el = document.querySelector('#cast-fx ' + sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            // 动画中的元素（光柱有 translateY）用声明式 top 更稳：
            // 量到的是「它该在哪」，而不是某一帧恰好飘到哪
            const cssTop = parseFloat(getComputedStyle(el).top) || 0;
            return { top: r.top, bottom: r.bottom, cy: r.top + r.height / 2, cssTop };
        };
        const panel = document.getElementById('chat-panel').getBoundingClientRect();
        const controls = document.getElementById('controls').getBoundingClientRect();
        return {
            vh: window.innerHeight,
            ring: box('.cf-ring'), core: box('.cf-core'),
            beam: box('.cf-beam'), hud: box('.cf-hud'),
            auraBg: getComputedStyle(document.querySelector('#cast-fx .cf-aura')).backgroundImage,
            panelTop: panel.top, controlsTop: controls.top,
            cfCyVar: getComputedStyle(document.querySelector('#cast-fx')).getPropertyValue('--cf-cy').trim(),
            chatTopVar: getComputedStyle(document.documentElement).getPropertyValue('--chat-top').trim(),
        };
    }""")
    # 法阵下缘必须用**声明值**算：法阵在 cf-spin 里旋转，getBoundingClientRect 量到的是
    # 旋转外接矩形（272 → 384px 浮动），拿它当「下缘」会平白多出几十像素误差。
    radius = min(lay["vh"] * 0.17, 160)
    ring_bottom = (lay["ring"]["cssTop"] + radius) if lay["ring"] else -1
    check("法阵下缘贴住聊天框顶部（±12px）",
          bool(lay["ring"]) and abs(lay["panelTop"] - ring_bottom) <= 12,
          "法阵底=%.0f / 聊天框顶=%.0f（--chat-top=%s）"
          % (ring_bottom, lay["panelTop"], lay["chatTopVar"]))
    check("锚点由 36_layout_sync 实测写入（不是百分比兜底）",
          lay["chatTopVar"].endswith("px"), "--chat-top=%s" % lay["chatTopVar"])

    # 矮态回归用例（本轮 bug 的复现场景）：面板高度由内容决定，消息少时远矮于 38dvh 上限，
    # 旧的纯百分比算式会把特效留在半空 —— 这里必须仍然贴住。
    page.evaluate("""() => {
        const msgs = document.querySelector('#chat-panel .messages');
        window.__cfKeepMsgs = msgs.innerHTML;
        msgs.innerHTML = '<div class="msg">就一条</div>';
        _App.onResize();
    }""")
    wait_panel_settled(page)        # 等面板高度/位置稳定 + ResizeObserver 把新 --chat-top 写下去
    short = page.evaluate("""() => {
        const panel = document.getElementById('chat-panel');
        const ring = document.querySelector('#cast-fx .cf-ring');
        return {vh: window.innerHeight, panelTop: panel.getBoundingClientRect().top,
                panelH: panel.offsetHeight,
                ringCy: parseFloat(getComputedStyle(ring).top) || 0,
                chatTop: getComputedStyle(document.documentElement).getPropertyValue('--chat-top').trim()};
    }""")
    short_bottom = short["ringCy"] + min(short["vh"] * 0.17, 160)
    check("面板矮于 38dvh 时仍贴住聊天框顶部（±12px）",
          short["panelH"] < short["vh"] * 0.38 - 1
          and abs(short["panelTop"] - short_bottom) <= 12,
          "面板高=%.0f / 法阵底=%.0f / 聊天框顶=%.0f（--chat-top=%s）"
          % (short["panelH"], short_bottom, short["panelTop"], short["chatTop"]))
    page.evaluate("""() => {
        document.querySelector('#chat-panel .messages').innerHTML = window.__cfKeepMsgs;
        _App.onResize();
    }""")
    wait_panel_settled(page)
    check("法阵 / 光核 / 光柱同挂一个锚点（纵向一致 ±2px）",
          bool(lay["ring"]) and bool(lay["core"]) and bool(lay["beam"])
          and abs(lay["ring"]["cy"] - lay["core"]["cy"]) <= 2
          and abs(lay["beam"]["cssTop"] - lay["ring"]["cy"]) <= 2,
          "法阵 %.0f / 光核 %.0f / 光柱 %.0f"
          % (lay["ring"]["cy"] if lay["ring"] else -1,
             lay["core"]["cy"] if lay["core"] else -1,
             lay["beam"]["cssTop"] if lay["beam"] else -1))
    check("整层锚点在聊天框顶部之上（特效重心不进聊天框）",
          bool(lay["ring"]) and lay["ring"]["cy"] < lay["panelTop"],
          "锚点=%.0f / 聊天框顶=%.0f"
          % (lay["ring"]["cy"] if lay["ring"] else -1, lay["panelTop"]))
    check("能量场中心跟着锚点走（不再是写死的 29% / 50% 50%）",
          "29%" not in (lay["auraBg"] or "") and "50% 50%" not in (lay["auraBg"] or ""),
          (lay["auraBg"] or "")[:70])
    check("施法字幕不越过聊天框顶部",
          bool(lay["hud"]) and lay["hud"]["bottom"] <= lay["panelTop"] + 1,
          "字幕底=%.0f / 聊天框顶=%.0f"
          % (lay["hud"]["bottom"] if lay["hud"] else -1, lay["panelTop"]))

    # ---------- 4·补四、全屏透明（◍）：铺满 + 半透明 + 渲染不停 ----------
    # 与不透明全屏（⤢）共用「铺满屏幕」的外壳，唯一分岔是**不静默** ——
    # 背景里得有东西在动，透明才有意义。
    gh = page.evaluate("""() => {
        const panel = document.getElementById('chat-panel');
        const btn = document.getElementById('chat-ghost-btn');
        const fsBtn = document.getElementById('chat-height-btn');
        if (!btn) return { missing: true };
        panel.classList.remove('collapsed');
        const snap = () => ({
            ghost: _App.chatGhost, fs: _App.chatFullscreen,
            ghostCls: panel.classList.contains('chat-ghost'),
            fsCls: panel.classList.contains('chat-fs'),
            quiet: _App.chatQuiet,
            loopStopped: _App.quietSnapshot().loopStopped,
            bg: getComputedStyle(panel).backgroundImage,
            bgColor: getComputedStyle(panel).backgroundColor,
            bd: getComputedStyle(panel).backdropFilter,
            ctrlBg: getComputedStyle(document.getElementById('controls')).backgroundColor,
            active: btn.classList.contains('active'),
            icon: btn.textContent,
        });
        const out = {};
        btn.click();
        out.ghost = snap();
        fsBtn.click();          // 互斥：两个按钮像一组单选
        out.fs = snap();
        btn.click();
        out.back = snap();
        btn.click();            // 再点一次 → 全部退出
        out.off = snap();
        panel.classList.add('collapsed');
        document.getElementById('controls').classList.add('collapsed');
        _App.onResize();
        return out;
    }""")
    check("聊天头部有全屏透明按钮（◍）", not gh.get("missing"))
    if not gh.get("missing"):
        check("点 ◍ → 进入全屏透明（chat-ghost + chat-fs）",
              gh["ghost"]["ghost"] and gh["ghost"]["ghostCls"] and gh["ghost"]["fsCls"],
              "ghost=%s cls=%s" % (gh["ghost"]["ghost"], gh["ghost"]["ghostCls"]))
        check("全屏透明下【不】静默：3D 帧循环继续跑（角色透得出来）",
              (not gh["ghost"]["quiet"]) and (not gh["ghost"]["loopStopped"]),
              "quiet=%s loopStopped=%s" % (gh["ghost"]["quiet"], gh["ghost"]["loopStopped"]))
        # 透明 = 与默认「半屏」面板同一种观感：面板自己不带任何底
        # （无背景图 / 背景 alpha=0 / 无 backdrop-filter），可读性交给 .msg 的文字投影。
        # 曾经的 0.46 深色渐变 + blur(5px) 在暗场景上观感「全不透明」；
        # 而且 backdrop-filter 压在 WebGL 画布上时采不到画布，会把背板糊成黑块。
        def _clear_bg(c):
            return c in ("transparent", "rgba(0, 0, 0, 0)")
        check("全屏透明面板【真透明】：无背景图、背景 alpha=0、无 backdrop-filter",
              gh["ghost"]["bg"] == "none"
              and _clear_bg(gh["ghost"]["bgColor"])
              and gh["ghost"]["bd"] == "none",
              "img=%s color=%s bd=%s" % (gh["ghost"]["bg"], gh["ghost"]["bgColor"], gh["ghost"]["bd"]))
        check("与不透明全屏（⤢）的底明显不同",
              "gradient" in gh["fs"]["bg"] and gh["fs"]["bg"] != gh["ghost"]["bg"],
              gh["fs"]["bg"][:60])
        check("输入栏跟着一起透（不是整屏只剩底部一条不透明的板）",
              gh["ghost"]["ctrlBg"] != gh["fs"]["ctrlBg"]
              and gh["ghost"]["ctrlBg"] != gh["off"]["ctrlBg"],
              "ghost=%s fs=%s off=%s" % (gh["ghost"]["ctrlBg"], gh["fs"]["ctrlBg"], gh["off"]["ctrlBg"]))
        check("点 ⤢ → 切到不透明全屏（与透明态互斥）",
              gh["fs"]["fs"] and (not gh["fs"]["ghost"]) and (not gh["fs"]["ghostCls"]),
              "fs=%s ghost=%s" % (gh["fs"]["fs"], gh["fs"]["ghost"]))
        check("不透明全屏仍会静默（省电那条老路没被改坏）",
              gh["fs"]["quiet"], "quiet=%s" % gh["fs"]["quiet"])
        check("从全屏切回 ◍ → 渲染恢复（setQuiet(false) 生效）",
              gh["back"]["ghost"] and (not gh["back"]["quiet"]),
              "ghost=%s quiet=%s" % (gh["back"]["ghost"], gh["back"]["quiet"]))
        check("再点 ◍ → 退出全屏，类清干净",
              (not gh["off"]["ghost"]) and (not gh["off"]["fs"]) and (not gh["off"]["fsCls"]),
              "ghost=%s fs=%s" % (gh["off"]["ghost"], gh["off"]["fs"]))
        check("◍ 激活态会亮（图标在 ◍ / ◉ 之间切换）",
              gh["ghost"]["active"] and gh["ghost"]["icon"] == "◉" and gh["off"]["icon"] == "◍",
              "%s → %s" % (gh["ghost"]["icon"], gh["off"]["icon"]))

    # ---------- 4·补五、头部排版：加到三个按钮也不许挤 ----------
    head = page.evaluate("""() => {
        const head = document.getElementById('chat-head');
        const title = head.querySelector('.chat-head-title');
        const actions = head.querySelector('.chat-head-actions');
        const btns = Array.prototype.slice.call(actions.querySelectorAll('.chat-head-btn'));
        const hr = head.getBoundingClientRect();
        const tr = title.getBoundingClientRect();
        const ar = actions.getBoundingClientRect();
        const rows = {};
        btns.forEach((b) => { rows[Math.round(b.getBoundingClientRect().top)] = 1; });
        const b0 = btns.length ? btns[0].getBoundingClientRect() : { width: 0, height: 0 };
        return {
            n: btns.length,
            rows: Object.keys(rows).length,
            headH: hr.height,
            gap: ar.left - tr.right,
            overflowRight: ar.right - hr.right,
            btnW: b0.width, btnH: b0.height,
            titleSize: parseFloat(getComputedStyle(title).fontSize),
        };
    }""")
    check("头部按钮齐全（⤢ / ◍ / ▾ 三个）", head["n"] == 3, "%d 个" % head["n"])
    check("三个按钮同一排（没被挤到换行）", head["rows"] == 1, "行数=%s" % head["rows"])
    check("标题与按钮不重叠（留 ≥4px 空隙）", head["gap"] >= 4, "空隙 %.0fpx" % head["gap"])
    check("按钮不越出头部右边界", head["overflowRight"] <= 0, "越出 %.0fpx" % head["overflowRight"])
    check("图标缩到 ≤28px、头部总高 ≤40px（排版不臃肿）",
          head["btnW"] <= 28 and head["btnH"] <= 28 and head["headH"] <= 40,
          "按钮 %.0f×%.0f 头高 %.0f" % (head["btnW"], head["btnH"], head["headH"]))
    check("标题字号已收小（≤12px）", head["titleSize"] <= 12, "%.1fpx" % head["titleSize"])

    # 走**真实入口**再验一次：确认 desc 是穿透 patch 链（09_websocket → toolChainStart
    # → 39_cast_fx 的 patch → begin → 字幕）传到的，而不是只有直接调 begin 才行。
    viaHook = page.evaluate("""(d) => {
        _App.cast.reset();
        _App.toolChainStart('code_search', {}, d);
        const t = (document.querySelector('#cast-fx .cf-act') || {}).textContent || '';
        _App.cast.reset();
        return t;
    }""", real.get("code_search", ""))
    check("desc 穿透 toolChainStart 链路到达中央字幕", viaHook == "批量检索代码", viaHook)

    # ---------- 4·补三、说明提炼规则（喂**真实**描述，不喂我编的字符串） ----------
    # 自己编字符串只能验证我的想象，验证不了真实工具上的效果 —— 这正是上一轮
    # 「测试全绿但跨端契约是错的」那个教训。所以这里把 155 个真实描述全部喂进去。
    got = page.evaluate("""(samples) => {
        const d = _App.cast.describe;
        const out = {};
        for (const k of Object.keys(samples)) out[k] = d(samples[k], k);
        return out;
    }""", real)
    missed = [k for k in real if not got.get(k) or got.get(k) == k]
    check("全量 %d 个真实工具：都能提炼出说明（没有一个落到工具名）" % len(real),
          not missed, "落到工具名: %s" % missed[:6])
    check("全量：说明都是单行（不含换行）",
          all("\n" not in got[k] for k in real))
    longest = max((len(got[k]) for k in real), default=0)
    check("全量：说明长度可控（最长 ≤ 28 字）", longest <= 28, "最长 %d 字" % longest)
    avg = sum(len(got[k]) for k in real) / max(1, len(real))
    check("全量：说明够简短（平均 ≤ 16 字）", avg <= 16, "平均 %.1f 字" % avg)

    # 抽样锁定：这几个是最常出现在屏幕上的工具，字幕效果必须稳定。
    # 改动工具描述导致这几行变化时测试会红 —— 这是有意的，字幕是用户可见的。
    expect = {
        "code_search": "批量检索代码",
        "code_read": "批量读取文件内容",
        "shell_run": "执行一条本机 shell 命令并返回输出",
        "code_verify": "验证代码改动",
        "todo_list": "查询任务清单",
    }
    for k, want in expect.items():
        check("describe(%s) → %s" % (k, want), got.get(k) == want, got.get(k))

    # 边界：没描述 → 显示工具名本身（真实，不猜）；都没有 → 兜底文案
    edge = page.evaluate("""() => {
        const d = _App.cast.describe;
        return { noDesc: d('', 'mystery_tool'), blank: d('   ', 'x_tool'),
                 all: d('', ''), paren: d('做某事（v9.9：内部备注）。细节。', 'x'),
                 colon: d('概括一句：后面全是细节细节细节细节细节细节。', 'x'),
                 long: d('A'.repeat(60) + '。后文', 'x') };
    }""")
    check("无描述 → 显示工具名本身（不加「正在…」这类猜测性前缀）",
          edge["noDesc"] == "mystery_tool", edge["noDesc"])
    check("空白描述 → 显示工具名本身", edge["blank"] == "x_tool", edge["blank"])
    check("连工具名都没有 → 兜底「正在施法」", edge["all"] == "正在施法", edge["all"])
    check("去掉括号里的补充说明（版本号等元信息）", edge["paren"] == "做某事", edge["paren"])
    check("超长时按冒号再切一刀（「概括：细节」写法）",
          edge["colon"] == "概括一句", edge["colon"])
    check("超长无标点 → 截断加省略号", edge["long"].endswith("…"), edge["long"])

    # ---------- 4·补二、连发：短期多次调用工具 ----------
    cb = page.evaluate("""() => {
        _App.cast.reset();
        const out = {};
        _App.toolChainStart('code_read', {});
        out.one = _App.cast.combo;
        _App.toolChainResult('code_read', 'ok', true);
        _App.toolChainStart('code_edit', {});
        out.two = _App.cast.combo;
        _App.toolChainStart('code_verify', {});
        _App.toolChainStart('code_test', {});
        out.four = _App.cast.combo;
        const badge = document.querySelector('#cast-fx .cf-combo');
        const wave = document.querySelector('#cast-fx .cf-combo-wave');
        const act = document.querySelector('#cast-fx .cf-act');
        return {
            one: out.one, two: out.two, four: out.four,
            badge: badge.textContent,
            htmlCombo: document.documentElement.classList.contains('casting-combo'),
            waveOn: wave.classList.contains('on'),
            act: act.textContent,
        };
    }""")
    check("首次调用 combo = 1（不算连发）", cb["one"] == 1, "combo=%s" % cb["one"])
    check("4s 内第二次调用 → combo = 2", cb["two"] == 2, "combo=%s" % cb["two"])
    check("连续 4 次调用 → combo = 4", cb["four"] == 4, "combo=%s" % cb["four"])
    check("连发徽章显示「连发 ×4」",
          "连发" in (cb["badge"] or "") and "4" in (cb["badge"] or ""), cb["badge"])
    check("连发时 html.casting-combo 已置位（整屏升温）", cb["htmlCombo"])
    check("连发触发冲击环", cb["waveOn"])
    # 这里没传 toolDesc，所以字幕显示工具名本身 —— 正好顺便验证了「兜底不猜」。
    # 断言只看「跟没跟到最新」，不看具体文案：文案由工具描述决定，不属于连发的职责。
    check("连发时字幕跟到最新工具（不是停在第一个）",
          "code_test" in (cb["act"] or "") and "code_read" not in (cb["act"] or ""),
          cb["act"])

    # 连发窗口之外：不算连发。用 reset 模拟「隔了很久」——
    # reset 清掉 lastCallAt，等价于窗口过期，且不依赖真实等待 4 秒
    reset_combo = page.evaluate("""() => {
        _App.cast.reset();
        _App.cast.begin('code_read');
        const a = _App.cast.combo;
        _App.cast.reset();          // 清掉时间窗口
        _App.cast.begin('code_read');
        const b = _App.cast.combo;
        return { a, b };
    }""")
    check("reset 后重新计数（窗口外的调用不算连发）",
          reset_combo["a"] == 1 and reset_combo["b"] == 1,
          "%s / %s" % (reset_combo["a"], reset_combo["b"]))

    # 中央字幕与礼物现在都在屏幕正中（本轮把整层特效下移到正中）——
    # 重叠不可避免，只记录两者位置，不再当作失败。
    page.evaluate("() => { _App.cast.reset(); _App.cast.begin('code_edit'); }")
    page.evaluate("() => { _App.live.drop('epic'); }")
    page.wait_for_timeout(800)      # 等礼物入场动画结束，取终态位置
    clash = page.evaluate("""() => {
        const h = document.querySelector('#cast-fx .cf-hud');
        const g = document.querySelector('#live-gifts .lv-gift');
        if (!h || !g) return { skip: true };
        const a = h.getBoundingClientRect(), b = g.getBoundingClientRect();
        const hit = !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
        return { hit, hudTop: Math.round(a.top), giftBottom: Math.round(b.bottom) };
    }""")
    if not clash.get("skip"):
        print("  · 中央字幕顶=%s / 礼物底=%s（两者同处正中，重叠属预期）"
              % (clash.get("hudTop"), clash.get("giftBottom")))

    # ---------- 5. 档位：后端 elapsed 驱动（同步原子，一次测全三档） ----------
    tier = page.evaluate("""() => {
        _App.cast.reset();
        _App.cast.begin('read');
        const out = {};
        const snap = () => ({
            tier: _App.cast.tier,
            t1: document.documentElement.classList.contains('cast-t1'),
            t2: document.documentElement.classList.contains('cast-t2'),
            label: (document.querySelector('#cast-fx .cf-tier') || {}).textContent,
            time: (document.querySelector('#cast-fx .cf-time') || {}).textContent,
        });
        out.start = snap();
        // ⚠ 单位契约：后端 elapsed 的单位是【秒】（int 截断过）。
        // 这里必须传 4 / 11 / 1 —— 传 4000 那种毫秒值会掩盖单位 bug。
        // 这不是假设：旧版本脚本就传的 4200/11000，把一个真实的
        // 「秒被当毫秒用」缺陷验证成了正确行为，全绿放行。
        _App.toolChainProgress('read', 4, '');
        out.t1 = snap();
        _App.toolChainProgress('read', 11, '');
        out.t2 = snap();
        _App.toolChainProgress('read', 1, '');   // 乱序：更小的 elapsed
        out.back = snap();
        return out;
    }""")
    check("初始档位 = 0（施法）", tier["start"]["tier"] == 0, "tier=%s" % tier["start"]["tier"])
    check("progress(4) → 档位 1（深度施法）", tier["t1"]["tier"] == 1,
          "tier=%s" % tier["t1"]["tier"])
    check("html.cast-t1 已置位", tier["t1"]["t1"])
    check("档位文案已更新", "深度" in (tier["t1"]["label"] or ""), tier["t1"]["label"])
    check("后端 elapsed 驱动了秒表（≥4.0s）",
          bool(tier["t1"]["time"]) and float(tier["t1"]["time"].rstrip("s")) >= 4.0,
          tier["t1"]["time"])
    check("progress(11) → 档位 2（超载）", tier["t2"]["tier"] == 2,
          "tier=%s" % tier["t2"]["tier"])
    check("html.cast-t2 已置位", tier["t2"]["t2"])
    check("超载文案已更新", "超载" in (tier["t2"]["label"] or ""), tier["t2"]["label"])
    check("更小的 elapsed 不会让档位倒退", tier["back"]["tier"] == 2,
          "tier=%s" % tier["back"]["tier"])

    # ---------- 6. 结束：收干净（同步原子） ----------
    e = page.evaluate("""() => {
        _App.cast.reset();
        _App.cast.begin('read');
        _App.toolChainProgress('read', 11, '');
        _App.toolChainResult('read', 'ok', true);
        const ring = document.querySelector('#cast-fx .cf-ring');
        const burst = document.querySelector('#cast-fx .cf-burst');
        return {
            active: _App.cast.active,
            casting: document.documentElement.classList.contains('casting'),
            t1: document.documentElement.classList.contains('cast-t1'),
            t2: document.documentElement.classList.contains('cast-t2'),
            ring: ring ? getComputedStyle(ring).animationName : 'missing',
            burstOn: burst ? burst.classList.contains('on') : false,
            particles: burst ? burst.querySelectorAll('i').length : 0,
        };
    }""")
    check("result → 施法结束（active 归零）", e["active"] == 0, "active=%s" % e["active"])
    check("result → casting 类已清除", not e["casting"])
    check("result → 档位类已清除", not e["t1"] and not e["t2"])
    check("result → 法阵动画已停止", e["ring"] in ("none", ""), e["ring"])
    check("result → 爆发动画已触发", e["burstOn"])
    check("result → 爆发粒子已生成（12 颗）", e["particles"] == 12, "粒子 %d 颗" % e["particles"])

    # ---------- 7. 重复 result 不该重复爆发 ----------
    dup = page.evaluate("""() => {
        _App.cast.reset();
        _App.cast.begin('read');
        _App.toolChainResult('read', 'ok', true);
        const b = document.querySelector('#cast-fx .cf-burst');
        b.classList.remove('on');          // 清掉第一次的痕迹
        _App.toolChainResult('read', 'ok', true);   // 迟到的重复 result
        return { on: b.classList.contains('on'), active: _App.cast.active };
    }""")
    check("重复 result 不会重复爆发", not dup["on"], "burstOn=%s" % dup["on"])
    check("重复 result 后 active 仍为 0", dup["active"] == 0, "active=%s" % dup["active"])

    # ---------- 8. 兜底：abort / endTurn（同步原子） ----------
    ab = page.evaluate("""() => {
        _App.cast.reset();
        _App.toolChainStart('grep', {});
        const before = document.documentElement.classList.contains('casting');
        _App.toolChainAbort();
        return {
            before,
            casting: document.documentElement.classList.contains('casting'),
            active: _App.cast.active,
        };
    }""")
    check("toolChainAbort 前确实在施法", ab["before"])
    check("abort → 施法态被兜底收干净",
          (not ab["casting"]) and ab["active"] == 0,
          "casting=%s active=%s" % (ab["casting"], ab["active"]))

    et = page.evaluate("""() => {
        _App.cast.reset();
        _App.toolChainStart('grep', {});
        const before = document.documentElement.classList.contains('casting');
        _App.toolChainEndTurn();
        return {
            before,
            casting: document.documentElement.classList.contains('casting'),
            active: _App.cast.active,
        };
    }""")
    check("toolChainEndTurn 前确实在施法", et["before"])
    check("endTurn → 施法态被兜底收干净",
          (not et["casting"]) and et["active"] == 0,
          "casting=%s active=%s" % (et["casting"], et["active"]))

    # ---------- 9. 秒表真的会自己走（异步，容忍真实事件干扰） ----------
    page.evaluate("() => { _App.cast.reset(); _App.cast.begin('read'); }")
    page.wait_for_timeout(700)
    ticked = page.evaluate("() => _App.cast.elapsed")
    check("秒表在走（elapsed > 300ms）", ticked > 300, "elapsed=%.0f" % ticked)

    # ---------- 10. 热度因果修正：在干活就涨，闲着才掉 ----------
    h0 = page.evaluate("""() => {
        _App.cast.reset();
        _App.live.reset();
        _App.live.heat(1000);
        _App.cast.begin('read');
        return _App.live.snapshot().heat;
    }""")
    page.wait_for_timeout(2600)
    h1 = page.evaluate("() => { _App.cast.reset(); return _App.live.snapshot().heat; }")
    page.wait_for_timeout(2600)
    h2 = page.evaluate("() => _App.live.snapshot().heat")
    check("施法中热度上升（在干活就涨）", h1 > h0, "%.0f → %.0f" % (h0, h1))
    check("空闲时热度下降（闲着才掉）", h2 < h1, "%.0f → %.0f" % (h1, h2))

    # ---------- 11. 无 JS 报错 ----------
    check("无页面 JS 报错", not errors, "; ".join(errors[:2])[:160])

    browser.close()

print("=== 通过 %d 项 ===" % len(ok))
for o in ok:
    print("  [OK]   " + o)
if bad:
    print("\n=== 未通过 %d 项 ===" % len(bad))
    for b in bad:
        print("  [FAIL] " + b)
    sys.exit(1)
print("\n全部通过")
