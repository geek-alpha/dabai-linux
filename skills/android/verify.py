#!/usr/bin/env python3
"""像素级验证 —— 区域定位 + 颜色语义判据。

存在的理由：整屏像素 diff 在视频/动画页上会被噪声淹没（实测 2.5 秒间隔两帧
差异占全屏 42.6%），信号永远小于噪声；而「某颜色在某控件区域内的像素数」与
播放无关——B站点赞按钮的粉 #FB7299 在 like_icon 区域 0（未赞）→ 5094（已赞）
→ 0（取消），三态可复现。

定位走资源 id 而不是文字：自绘页（B站竖版、微信）读不到文字，id/like_layout
照样能定位，且与分辨率、语言、字号无关。

用法：
  verify like_layout                 报区域 + 主色
  verify like_layout #FB7299         报目标色像素数，并与上次比
  verify 911,1017,1079,1155 #FB7299  直接给区域
  verify like_layout #FB7299 --reset 清基线，重新记
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

STATE = Path("/home/wxf/dabai/data/android/verify_state.json")
_BOX_RE = re.compile(r"^\s*(\d+)\s*[, ]\s*(\d+)\s*[, ]\s*(\d+)\s*[, ]\s*(\d+)\s*$")


def _ui():
    """延迟导入 adb_ui：模块级 import 会与 adb_ui 的 verify 子命令循环。"""
    import adb_ui
    return adb_ui


def hex2rgb(s: str):
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return None
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def shot_rgb(box=None):
    """截屏并裁出区域，返回 numpy 数组 (h, w, 3)。"""
    import numpy as np
    from PIL import Image
    U = _ui()
    raw = U.shb("exec-out", "screencap", "-p", timeout=30)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception:
        img = Image.open(io.BytesIO(raw.replace(b"\r\n", b"\n")))
    img = img.convert("RGB")
    if box:
        img = img.crop(box)
    return np.asarray(img).astype(int)


def top_colors(arr, n=3):
    """区域主色 top-N。量化到 16 级再统计：抗锯齿会把一个颜色打成十几个邻近色，
    不量化的话「主色」全是占比 1% 的碎片。"""
    import numpy as np
    flat = arr.reshape(-1, 3)
    if flat.shape[0] > 200000:
        flat = flat[:: flat.shape[0] // 200000 + 1]
    keys, counts = np.unique(flat // 16, axis=0, return_counts=True)
    total = int(flat.shape[0])
    order = np.argsort(-counts)[:n]
    out = []
    for i in order:
        c = tuple(int(v) * 16 + 8 for v in keys[i])
        out.append(("#%02X%02X%02X" % c, int(counts[i]), int(counts[i]) / total))
    return out


def locate(kw: str):
    """按资源名/文字定位控件框，返回 (box, 描述, 通道)；找不到返回 (None, 原因, "")。

    uiautomator 通道优先（能同时看到文字和 rid），viewtree 兜底（只有 id/类名）。
    多命中时取面积最小的：容器套容器，叶子才是那个按钮。
    """
    U = _ui()
    k = kw.lower()
    nodes = U.nodes_now()
    cand = [n for n in nodes if k in n["rid"].lower() or k in n["text"].lower()
            or k in n["desc"].lower()]
    if cand:
        def rank(n):
            rid = n["rid"].lower()
            # 资源名尾段完全相等最准；其次文本完全相等；同级按面积小者优先
            if rid.endswith("/" + k) or rid == k:
                exact = 0
            elif n["text"].lower() == k or n["desc"].lower() == k:
                exact = 1
            else:
                exact = 2
            b = n["b"]
            return (exact, (b[2] - b[0]) * (b[3] - b[1]))
        n = min(cand, key=rank)
        b = n["b"]
        lab = n["text"] or n["desc"] or n["rid"].split("/")[-1]
        return tuple(b), f"「{lab}」{n['cls'].split('.')[-1]}", "uiautomator"
    win, vt = U.viewtree_nodes()
    if vt is None:
        return None, "两条通道都没读到控件（dump 失败？）", ""
    pkg = (win or "").split("/")[0]
    cand = []
    for n in vt:
        nm = U._resid_name(pkg, n["id"]) or ""
        if k in nm.lower() or k in (n["id"] or "").lower():
            cand.append((n, nm))
    if cand:
        n, nm = min(cand, key=lambda t: t[0]["w"] * t[0]["h"])
        import viewtree as V
        return tuple(n["b"]), f"{nm or n['id']} {V.short(n['cls'])}", "viewtree"
    return None, f"没找到「{kw}」（uiautomator 和 viewtree 都没有）", ""


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def do_verify(kw: str = "", color: str = "", tol: int = 50, label: str = "",
              box: str = "", reset: bool = False) -> str:
    """量一个区域：报主色；给了 color 就报该色像素数并与上次比。

    第一次调用记基线，之后每次报 delta —— 「点前一次、点后一次」就是完整验证。
    """
    U = _ui()
    win = U.norm_win(U.cur_window())
    if box:
        m = _BOX_RE.match(box)
        if not m:
            return "✗ box 要写成 x1,y1,x2,y2"
        bx = tuple(int(v) for v in m.groups())
        desc, chan = f"box {bx[0]},{bx[1]}-{bx[2]},{bx[3]}", "手动"
    elif kw:
        bx, desc, chan = locate(kw)
        if bx is None:
            return f"✗ {desc}"
    else:
        w, h = U.screen_size()
        bx, desc, chan = (0, 0, w, h), "全屏", "全屏"
    if bx[2] <= bx[0] or bx[3] <= bx[1]:
        return f"✗ 区域无效：{bx}"
    arr = shot_rgb(bx)
    if arr is None or arr.size == 0:
        return "✗ 截图失败"
    total = int(arr.shape[0] * arr.shape[1])
    lines = [f"区域 {desc} {bx[0]},{bx[1]}-{bx[2]},{bx[3]} "
             f"{bx[2] - bx[0]}x{bx[3] - bx[1]} [{chan}]",
             "主色 " + " / ".join(f"{c} {p * 100:.0f}%" for c, _, p in top_colors(arr))]
    key = label or f"{win}|{kw or box or 'full'}|{color or '-'}"
    st = _load()
    rgb = hex2rgb(color)
    if not rgb:
        if reset:
            st.pop(key, None)
            _save(st)
            lines.append(f"基线已清除 {key}")
        return "\n".join(lines)
    d = (abs(arr[:, :, 0] - rgb[0]) + abs(arr[:, :, 1] - rgb[1]) + abs(arr[:, :, 2] - rgb[2]))
    cnt = int((d <= tol).sum())
    lines.append(f"目标色 {color} 容差 {tol} → {cnt} 像素（{cnt / total * 100:.1f}%）")
    old = st.get(key)
    lo = hi = cnt
    if reset or not old:
        lines.append(f"基线已记录 {key} = {cnt}")
    else:
        base = old.get("cnt", 0)
        # 同一状态也会波动：视频在播时同一颗心的粉色像素实测 4968→5134（+3.3%），
        # 拿「不等就算变化」会把噪声报成状态改变。12% 以内算同一状态，只更新噪声带。
        rel = abs(cnt - base) / max(base, 1)
        if rel < 0.12:
            lo = min(old.get("lo", base), cnt)
            hi = max(old.get("hi", base), cnt)
            lines.append(f"同一状态（{base} → {cnt}，波动 {cnt - base:+d}，"
                         f"噪声带 {lo}~{hi}）")
        else:
            lines.append(f"状态变了：{base} → {cnt}（{cnt - base:+d}）")
    st[key] = {"cnt": cnt, "lo": lo, "hi": hi, "ts": time.time(),
               "desc": desc, "win": win}
    _save(st)
    return "\n".join(lines)


def main(argv=None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    rest, reset, tol = [], False, 50
    i = 0
    while i < len(argv):
        if argv[i] == "--reset":
            reset = True
        elif argv[i] == "--tol" and i + 1 < len(argv):
            i += 1
            tol = int(argv[i])
        else:
            rest.append(argv[i])
        i += 1
    box = rest[0] if rest and _BOX_RE.match(rest[0]) else ""
    kw = "" if box else (rest[0] if rest else "")
    color = next((x for x in rest[1:] if x.startswith("#")), "")
    print(do_verify(kw=kw, box=box, color=color, tol=tol, reset=reset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
