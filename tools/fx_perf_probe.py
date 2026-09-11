# -*- coding: utf-8 -*-
"""氛围层性能探针（Playwright + 系统 Edge）。

用户反馈「容易拥挤、有点卡」。拥挤是主观的，卡是客观的 —— 先把客观的那半量出来，
再决定砍谁。这个脚本回答三个问题：

  1. 页面上到底有多少个动画在跑？（数量本身不是问题，问题是有多少是「常驻」的）
  2. 谁在拖帧？逐层开关（全息/灯光/氛围/施法）对比帧时间，定位真正的成本来源
  3. 有没有长任务（long task）？主线程被占住才是「卡」的直接体感

指标说明：
  · frame p95 —— 95% 的帧耗时。平均值会被少数好帧美化，p95 才反映真实卡顿
  · jank 率   —— 超过 20ms 的帧占比（60fps 的预算是 16.7ms）
  · anims     —— document.getAnimations() 里正在跑的数量

用法：python tools/fx_perf_probe.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "https://localhost:8000/"

MEASURE = """
async () => {
  const raf = () => new Promise((r) => requestAnimationFrame(r));
  const deltas = [];
  let last = performance.now();
  const t0 = performance.now();
  while (performance.now() - t0 < 2000) {
    await raf();
    const now = performance.now();
    deltas.push(now - last);
    last = now;
  }
  deltas.sort((a, b) => a - b);
  const p = (q) => deltas[Math.min(deltas.length - 1, Math.floor(deltas.length * q))];
  const jank = deltas.filter((d) => d > 20).length;
  return {
    n: deltas.length,
    avg: +(deltas.reduce((a, b) => a + b, 0) / deltas.length).toFixed(2),
    p50: +p(0.5).toFixed(2),
    p95: +p(0.95).toFixed(2),
    max: +deltas[deltas.length - 1].toFixed(2),
    jankPct: +((jank / deltas.length) * 100).toFixed(1),
    anims: document.getAnimations().length,
  };
}
"""

# 逐层开关：定位成本到底在谁身上
LAYERS = [
    ("全部打开", []),
    ("关掉 41 全息舞台", ["holo-fx", "light-fx"]),
    ("再关掉 39 施法态", ["holo-fx", "light-fx", "cast-fx"]),
    ("再关掉 35 街机氛围", ["holo-fx", "light-fx", "cast-fx", "arcade-fx"]),
]


def count_ambient(page):
    return page.evaluate("""() => {
        const root = document.getElementById('stage') || document.body;
        const all = [...root.querySelectorAll('*')];
        const anim = (el) => {
            const cs = getComputedStyle(el);
            return cs.animationName !== 'none' && cs.animationIterationCount !== '1';
        };
        const blend = (el) => getComputedStyle(el).mixBlendMode !== 'normal';
        const blur = (el) => (getComputedStyle(el).filter || '').includes('blur');
        const full = (el) => {
            const r = el.getBoundingClientRect();
            return r.width >= window.innerWidth * 0.9 && r.height >= window.innerHeight * 0.6;
        };
        return {
            total: all.length,
            ambient: all.filter(anim).length,
            blend: all.filter(blend).length,
            blur: all.filter(blur).length,
            fullBlend: all.filter((e) => blend(e) && full(e)).length,
            fullBlurAnim: all.filter((e) => blur(e) && anim(e)).length,
            fullBlendAnim: all.filter((e) => blend(e) && anim(e) && full(e)).length,
            perLayer: ['arcade-fx', 'light-fx', 'holo-fx', 'cast-fx'].map((id) => {
                const el = document.getElementById(id);
                if (!el) return [id, 0, 0];
                const kids = [...el.querySelectorAll('*')];
                return [id, kids.filter(anim).length, kids.length];
            }),
        };
    }""")


def inventory(page):
    # 常驻动画清单：谁在动、多快、多大、带不带 blend/blur。
    # 只有看清每一个动效，才能有理有据地砍 —— 否则就是凭感觉删，删错还得回滚。
    # 注意区分「在动」和「挂着动画但已暂停」：后者不花帧，
    # 但会污染计数 —— 一个暂停的全屏动画和跑着的全屏动画，成本差一个数量级。
    return page.evaluate("""() => {
        const root = document.getElementById('stage') || document.body;
        const vw = innerWidth, vh = innerHeight;
        const rows = [];
        for (const el of root.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            if (cs.animationName === 'none' || cs.animationIterationCount === '1') continue;
            const r = el.getBoundingClientRect();
            // 面积按与视口的交集算：滑出屏幕的元素动画还在跑，但不花绘制成本
            const iw = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
            const ih = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
            rows.push({
                sel: (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string'
                    ? '.' + el.className.trim().split(/\\s+/)[0] : ''),
                anim: cs.animationName,
                dur: cs.animationDuration,
                area: Math.round((iw * ih) / (vw * vh) * 100),
                blend: cs.mixBlendMode !== 'normal',
                blur: (cs.filter || '').includes('blur'),
                paused: cs.animationPlayState === 'paused',
            });
        }
        rows.sort((a, b) => (b.area * (b.blend ? 2 : 1)) - (a.area * (a.blend ? 2 : 1)));
        return rows;
    }""")


def backdrop_list(page):
    # 常驻 backdrop-filter 清单。
    # 为什么单独查它：backdrop-filter 要「读取背后的像素再模糊」，而背后是
    # 一直在动的 WebGL 画布 —— 等于每一帧都要重新采样+模糊一次。
    # 面积按**与视口的交集**算，不是元素自身尺寸：
    # 一个滑出屏幕的面板自身 29% 大，但实际占 0% —— 尺寸算面积会自己骗自己。
    return page.evaluate("""() => {
        const vw = innerWidth, vh = innerHeight;
        const rows = [];
        for (const el of document.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            const bf = cs.backdropFilter && cs.backdropFilter !== 'none' ? cs.backdropFilter
                : (cs.webkitBackdropFilter && cs.webkitBackdropFilter !== 'none'
                    ? cs.webkitBackdropFilter : '');
            if (!bf) continue;
            const r = el.getBoundingClientRect();
            const iw = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
            const ih = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
            const shown = cs.display !== 'none' && cs.visibility !== 'hidden'
                && parseFloat(cs.opacity) > 0.01;
            rows.push({
                sel: (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string'
                    ? '.' + el.className.trim().split(/\\s+/)[0] : ''),
                bf,
                area: Math.round((iw * ih) / (vw * vh) * 100),
                visible: shown && iw > 4 && ih > 4,
            });
        }
        rows.sort((a, b) => b.area - a.area);
        return rows;
    }""")


with __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True,
                                args=["--ignore-certificate-errors"])
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(7000)

    page.evaluate("""() => {
        const b = document.getElementById('status-badge');
        if (b) b.className = 'status-badge active';
        // 归零到空闲态：页面刚加载完可能正好在「思考/施法」中，
        // 不归零就会把运行期状态当成常态来量 —— 空闲态的常驻开销才是要盯的数。
        document.documentElement.className =
            document.documentElement.className.replace(/\\bcasting\\S*/g, '').trim();
        // 长任务观察：主线程被占住才是「卡」的直接体感
        window.__long = [];
        try {
            new PerformanceObserver((l) => {
                for (const e of l.getEntries()) window.__long.push(Math.round(e.duration));
            }).observe({ entryTypes: ['longtask'] });
        } catch (e) { /* 不支持就算了 */ }
    }""")
    page.wait_for_timeout(600)
    print("  空闲态 html class = %r" % page.evaluate("() => document.documentElement.className"))

    print("=== 氛围层性能探针 ===")
    c = count_ambient(page)
    print("  元素总数 %d ｜ 常驻动画 %d ｜ blend 层 %d ｜ blur 层 %d"
          % (c["total"], c["ambient"], c["blend"], c["blur"]))
    print("  其中：全屏 blend %d ｜ blur+动画 %d ｜ 全屏 blend+动画 %d"
          % (c["fullBlend"], c["fullBlurAnim"], c["fullBlendAnim"]))
    for lid, a, t in c["perLayer"]:
        print("    %-10s 动画 %2d / 子节点 %2d" % (lid, a, t))

    print("\n  常驻动画清单（按 面积×blend 排序，★ = 全屏且带混合 = 最贵的一类）:")
    inv = inventory(page)
    for r in inv:
        flag = "★" if (r["area"] >= 90 and r["blend"]) else " "
        print("   %s %-22s %-18s %-7s 面积%4d%%  %s%s%s"
              % (flag, r["sel"], r["anim"], r["dur"], r["area"],
                 "blend " if r["blend"] else "", "blur " if r["blur"] else "",
                 "[已暂停]" if r["paused"] else ""))
    running = [r for r in inv if not r["paused"]]
    print("  合计 %d 个挂着动画，其中**在跑**的 %d 个（暂停的不花帧）；"
          "全屏+blend 在跑的 %d 个"
          % (len(inv), len(running),
             sum(1 for r in running if r["area"] >= 90 and r["blend"])))

    print("\n  常驻 backdrop-filter 清单（背后是持续动画的画布 → 每帧都要重新模糊）:")
    bl = backdrop_list(page)
    vis_bl = [b for b in bl if b["visible"]]
    for b in bl:
        print("   %s %-26s %-22s 面积%4d%%"
              % ("●" if b["visible"] else " ", b["sel"], b["bf"], b["area"]))
    print("  共 %d 个（其中**可见**的 %d 个，合计约占视口 %d%%）"
          % (len(bl), len(vis_bl), sum(b["area"] for b in vis_bl)))

    print("\n  逐层开关（各测 2s）:")
    rows = []
    for label, ids in LAYERS:
        page.evaluate("""(ids) => {
            for (const id of ids) {
                const el = document.getElementById(id);
                if (el) el.style.display = 'none';
            }
        }""", ids)
        page.wait_for_timeout(400)
        r = page.evaluate(MEASURE)
        rows.append((label, r))
        print("    %-18s 帧 p50 %5.1fms  p95 %6.1fms  max %6.1fms  jank %4.1f%%  动画 %3d"
              % (label, r["p50"], r["p95"], r["max"], r["jankPct"], r["anims"]))

    longs = page.evaluate("() => window.__long || []")
    if longs:
        print("\n  长任务 %d 个，最长 %dms" % (len(longs), max(longs)))
    else:
        print("\n  长任务 0 个")

    browser.close()
