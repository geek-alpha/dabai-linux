# -*- coding: utf-8 -*-
"""待机动作自然度探针（Playwright + 系统 Edge）。

动作自然度是观感问题，但观感背后有两个可量的硬指标：

  1. 呼吸链相位差 —— spine / chest / upperChest / head 在主频处的相位。
     真人呼吸从腰腹往上传导，胸腔、锁骨、头依次滞后；三者同频同相时，
     躯干看起来就是「一整块板在前后摆」。
  2. 主频能量占比 —— 单一正弦 = 机械节拍，能量 100% 压在一个频率上；
     自然动作的能量会摊到多个非整数倍频率（且频率本身缓慢漂移）。

改前跑一次拿基线，改后跑一次做对比。

用法：
    venv\\Scripts\\python.exe tools/motion_naturalness_probe.py --sec 12
    venv\\Scripts\\python.exe tools/motion_naturalness_probe.py --sec 12 --out data/motion_probe_after.json
"""
import argparse
import json
import math
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = os.environ.get("MOTION_PROBE_URL", "https://localhost:8008/")
CHAIN = ["spine", "chest", "upperChest", "neck", "head"]
EXTRA = ["hips", "leftUpperArm", "rightUpperArm"]
KEYS = CHAIN + EXTRA

SAMPLE_JS = """
(sec) => new Promise(resolve => {
  const A = window._App;
  if (!A) { resolve({ err: 'no _App' }); return; }
  if (!A.vrmBones || !A.vrmBones.spine) {
    resolve({ err: 'no vrmBones', modelType: A.modelType || '-' });
    return;
  }
  const B = A.vrmBones;
  const keys = %s;
  const rows = [];
  const t0 = performance.now();
  function tick() {
    const now = performance.now();
    const rec = { t: (now - t0) / 1000 };
    for (let i = 0; i < keys.length; i += 1) {
      const b = B[keys[i]];
      if (b && b.rotation) rec[keys[i]] = [b.rotation.x, b.rotation.y, b.rotation.z];
    }
    rows.push(rec);
    if (now - t0 < sec * 1000) requestAnimationFrame(tick);
    else resolve({
      rows: rows,
      modelType: A.modelType, state: A.currentState,
      quiet: !!A.chatQuiet, mixamo: A._mixamoActiveClip || null,
    });
  }
  requestAnimationFrame(tick);
})
""" % json.dumps(KEYS)


def launch_kwargs():
    exe = os.environ.get("MOTION_PROBE_EXECUTABLE")
    args = [
        "--ignore-certificate-errors",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--enable-unsafe-swiftshader",
    ]
    if exe:
        return {"executable_path": exe, "headless": True, "args": args + ["--no-sandbox"]}
    return {"channel": "msedge", "headless": True, "args": args}


def resample(rows, key, axis, fs):
    """把不等间隔的 rAF 采样重采样到固定 fs 网格（线性插值）。"""
    pts = [(r["t"], r[key][axis]) for r in rows if key in r and len(r[key]) > axis]
    if len(pts) < 8:
        return None
    dur = pts[-1][0]
    n = int(dur * fs)
    if n < 16:
        return None
    out = []
    j = 0
    for i in range(n):
        tt = i / fs
        while j + 1 < len(pts) - 1 and pts[j + 1][0] < tt:
            j += 1
        t0, v0 = pts[j]
        t1, v1 = pts[j + 1]
        w = 0.0 if t1 <= t0 else (tt - t0) / (t1 - t0)
        w = max(0.0, min(1.0, w))
        out.append(v0 + (v1 - v0) * w)
    return out


def spectrum(series, fs, fmin=0.02, fmax=1.2, step=0.002):
    """扫频 DFT：返回 (主频, 主频幅度, 主频相位, 前3峰, 主频能量占比)。"""
    n = len(series)
    mean = sum(series) / n
    x = [v - mean for v in series]
    var = sum(v * v for v in x) / n
    if var <= 1e-12:
        return None
    peaks = []
    f = fmin
    while f <= fmax + 1e-9:
        re = im = 0.0
        for i, v in enumerate(x):
            a = 2 * math.pi * f * i / fs
            re += v * math.cos(a)
            im -= v * math.sin(a)
        peaks.append((f, math.hypot(re, im) * 2 / n, math.atan2(im, re)))
        f += step
    peaks.sort(key=lambda p: -p[1])
    # 局部极大值挑前 3 个互不邻近的峰（邻近 = 频率差 < 0.03Hz，同一峰的裙边）
    top = []
    for p in peaks:
        if all(abs(p[0] - q[0]) > 0.05 for q in top):
            top.append(p)
        if len(top) == 3:
            break
    f0, a0, ph0 = top[0]
    return {
        "f0": f0, "amp": a0, "phase": ph0,
        "period_s": 1.0 / f0 if f0 > 0 else 0.0,
        "top": [{"f": round(p[0], 4), "amp": round(p[1], 6)} for p in top],
        "fund_ratio": (a0 * a0 / 2) / var,
        "rms": math.sqrt(var),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=float, default=12.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--url", default=URL)
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs())
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(a.url, wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => window._App && window._App.vrmBones && window._App.vrmBones.spine",
                timeout=60000)
        except Exception as e:
            print("模型未就绪，探针无法采样：%s" % e)
            print("pageerror:", errs[:3])
            browser.close()
            return 2
        # headless 首屏会落在聊天全屏静默态（chat-quiet）——那时 three 的帧循环被
        # setAnimationLoop(null) 停掉，骨骼是常量，频谱分析只会拿到方差 0。
        # 采样前必须显式退静默并确认帧循环真的转起来。
        quiet = page.evaluate("""() => {
            const A = window._App;
            if (A && A.setQuiet) A.setQuiet(false);
            if (A && A.syncQuietLoop) A.syncQuietLoop();
            return { quiet: !!(A && A.chatQuiet), snap: A && A.quietSnapshot ? A.quietSnapshot() : null };
        }""")
        page.wait_for_timeout(2500)
        moved = page.evaluate("""() => new Promise(r => {
            const B = window._App.vrmBones;
            const a = B.spine.rotation.x;
            const t0 = performance.now();
            (function w() {
                if (Math.abs(B.spine.rotation.x - a) > 1e-6 || performance.now() - t0 > 3000) {
                    r({ moved: Math.abs(B.spine.rotation.x - a) > 1e-6, quiet: !!window._App.chatQuiet });
                } else requestAnimationFrame(w);
            })();
        })""")
        if not moved.get("moved"):
            print("骨骼在 3 秒内纹丝不动（quiet=%s）——帧循环没跑，频谱分析无意义"
                  % moved.get("quiet"))
            print("quiet 快照：%s" % (quiet.get("snap"),))
            browser.close()
            return 2
        res = page.evaluate(SAMPLE_JS, a.sec)
        browser.close()

    if not res or res.get("err"):
        print("采样失败：%s" % (res or {}))
        return 2

    rows = res["rows"]
    if rows:
        report_keys = sorted(k for k in rows[0] if k != "t")
    else:
        report_keys = []
    if len(rows) < 16:
        print("采样帧数过少（%d），headless 下帧循环可能被节流" % len(rows))
        return 2
    dur = rows[-1]["t"]
    fps = len(rows) / dur if dur > 0 else 0
    # 固定 30Hz 网格：改前/改后两次采样若各自取不同 fs，频率分辨率不同会污染对比
    fs = 30.0 if fps >= 25 else max(8.0, round(fps))

    report = {
        "url": a.url, "sec": dur, "frames": len(rows), "fps": round(fps, 2),
        "modelType": res.get("modelType"), "state": res.get("state"),
        "quiet": res.get("quiet"), "mixamo": res.get("mixamo"),
        "bones": {},
        "sampled_bones": report_keys,
    }

    print("采样：%.1fs / %d 帧 / %.1f fps   model=%s state=%s quiet=%s"
          % (dur, len(rows), fps, res.get("modelType"), res.get("state"), res.get("quiet")))
    print("")
    print("%-14s %-8s %-8s %-9s %-8s %s" % ("骨骼", "主频Hz", "周期s", "幅度rad", "主频占比", "前三峰Hz"))
    for k in KEYS:
        s = resample(rows, k, 0, fs)
        if not s:
            continue
        sp = spectrum(s, fs)
        if not sp:
            continue
        report["bones"][k] = sp
        print("%-14s %-8.4f %-8.2f %-9.5f %-8.2f %s"
              % (k, sp["f0"], sp["period_s"], sp["amp"], sp["fund_ratio"],
                 ",".join("%.3f" % q["f"] for q in sp["top"])))

    print("")
    print("呼吸链相位（以 spine 为基准，正 = 滞后）")
    base = report["bones"].get("spine")
    if base:
        for k in CHAIN[1:]:
            b = report["bones"].get(k)
            if not b:
                continue
            df = b["f0"] - base["f0"]
            dphi = b["phase"] - base["phase"]
            while dphi > math.pi:
                dphi -= 2 * math.pi
            while dphi < -math.pi:
                dphi += 2 * math.pi
            # sin(2πf(t-τ)) 在 f 处的 DFT 相位是 -2πfτ：负 Δφ 才是「滞后」
            lag_ms = -dphi / (2 * math.pi * base["f0"]) * 1000
            report["bones"][k]["lag_ms_vs_spine"] = lag_ms
            print("  %-12s Δf=%+.4f Hz   Δφ=%+.3f rad   滞后 %+7.1f ms"
                  % (k, df, dphi, lag_ms))

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\n结果已写入 %s" % a.out)
    if errs:
        print("\npageerror（前 3 条）：%s" % errs[:3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
