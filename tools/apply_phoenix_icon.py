#!/usr/bin/env python3
"""把某个图标变体铺到全部图标位（Web PWA + Android mipmap + SVG）。

三套形状库：
  --module v2      gen_phoenix_v2.py      V1~V4  凤凰造型
  --module robot   gen_phoenix_robot.py   R1~R4  机器人造型
  --module head    gen_phoenix_head.py    V1     V1 头部特写（居中放大）

用法：
  python tools/apply_phoenix_icon.py --module robot --variant r3            # 预览将写哪些文件
  python tools/apply_phoenix_icon.py --module robot --variant r3 --write    # 实际写入
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODULES = {"v2": "gen_phoenix_v2", "robot": "gen_phoenix_robot", "head": "gen_phoenix_head",
           "legacy": "gen_phoenix_legacy"}

WEB = {"icon-1024.png": 1024, "icon-512.png": 512, "icon-192.png": 192}
ANDROID = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ADAPTIVE_FG = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
FG_SAFE = 0.85
MASKABLE_PAD = 0.78


def load(name: str):
    return importlib.import_module(MODULES[name])


def _base(mod):
    """robot 模块的几何/渐变函数来自 gen_phoenix_v2（它 import 为 P）。"""
    return getattr(mod, "P", mod)


def render(mod, v, size: int, pad: float = 1.0, bg: bool = True) -> Image.Image:
    """两个模块的 render 签名一致（v, size, pad, bg），直接转发——不再各写一份。"""
    return mod.render(v, size, pad=pad, bg=bg)


def _norm(sh) -> tuple:
    """统一形状语法：v2 的洞是 (cx, cy, r)，robot 带类型标签 ("circle", ...) / ("rrect", ...) / ("poly", ...)。"""
    if sh[0] in ("circle", "rrect", "poly"):
        return tuple(sh)
    cx, cy, r = sh
    return ("circle", cx, cy, r)


def _expand(mod, v, key: str) -> list:
    """取某个形状层的全部形状（含镜像展开）。"""
    if key == "holes" and hasattr(mod, "holes_of"):
        return [_norm(s) for s in mod.holes_of(v)]
    if hasattr(mod, "shapes_of"):
        return [_norm(s) for s in mod.shapes_of(v, key)]
    return []


def _el(sh, fill: str, mod=None) -> str:
    if sh[0] == "circle":
        _, cx, cy, r = sh
        return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{fill}"/>'
    if sh[0] == "poly":
        _, pts, sm = sh
        if mod is not None:
            pts = _base(mod).cr(pts, closed=True, samples=sm)
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        return f'<polygon points="{d}" fill="{fill}"/>'
    _, x1, y1, x2, y2, r = sh
    return (f'<rect x="{x1:.2f}" y="{y1:.2f}" width="{x2 - x1:.2f}" '
            f'height="{y2 - y1:.2f}" rx="{r:.2f}" fill="{fill}"/>')


def svg(mod, v) -> str:
    base = _base(mod)
    d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in base.outline(v)) + " Z"
    adds = "".join(_el(s, "url(#fg)", mod) for s in _expand(mod, v, "adds"))
    holes = "".join(_el(s, "url(#bg)", mod) for s in _expand(mod, v, "holes"))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {base.S} {base.S}" width="{base.S}" height="{base.S}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#{base.BG_TOP[0]:02X}{base.BG_TOP[1]:02X}{base.BG_TOP[2]:02X}"/>
      <stop offset="1" stop-color="#{base.BG_BOT[0]:02X}{base.BG_BOT[1]:02X}{base.BG_BOT[2]:02X}"/>
    </linearGradient>
    <linearGradient id="fg" x1="0" y1="0" x2="0.12" y2="1">
      <stop offset="0" stop-color="#{base.FG_TOP[0]:02X}{base.FG_TOP[1]:02X}{base.FG_TOP[2]:02X}"/>
      <stop offset="1" stop-color="#{base.FG_BOT[0]:02X}{base.FG_BOT[1]:02X}{base.FG_BOT[2]:02X}"/>
    </linearGradient>
  </defs>
  <rect width="{base.S}" height="{base.S}" fill="url(#bg)"/>
  <path d="{d}" fill="url(#fg)"/>
  {adds}
  {holes}
</svg>
"""


def plan(mod, v, root: str) -> list:
    """返回 (输出路径, 生成函数) 清单——先规划再执行，方便 dry run。"""
    items = []
    web_dir = os.path.join(root, "web", "icons")
    for name, sz in WEB.items():
        items.append((os.path.join(web_dir, name),
                      lambda p, s=sz: render(mod, v, s).save(p)))
    items.append((os.path.join(web_dir, "icon-512-maskable.png"),
                  lambda p: render(mod, v, 512, pad=MASKABLE_PAD).save(p)))
    items.append((os.path.join(web_dir, "phoenix-icon.svg"),
                  lambda p: open(p, "w", encoding="utf-8").write(svg(mod, v))))

    res = os.path.join(root, "apps", "dabai-android", "app", "src", "main", "res")
    for dens, sz in ANDROID.items():
        d = os.path.join(res, f"mipmap-{dens}")
        items.append((os.path.join(d, "ic_launcher.png"),
                      lambda p, s=sz: render(mod, v, s).save(p)))
    for dens, sz in ADAPTIVE_FG.items():
        d = os.path.join(res, f"mipmap-{dens}")
        items.append((os.path.join(d, "ic_launcher_foreground.png"),
                      lambda p, s=sz: render(mod, v, s, pad=FG_SAFE, bg=False).save(p)))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="v2", choices=sorted(MODULES))
    ap.add_argument("--variant", default="v1")
    ap.add_argument("--root", default="/home/wxf/dabai")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    mod = load(a.module)
    v = mod.VARIANTS[a.variant.lower()]
    items = plan(mod, v, a.root)
    print(f"形状库: {a.module}  变体: {v['name']} — {v['note']}")
    for path, fn in items:
        if a.write:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fn(path)
            print("写出:", path, os.path.getsize(path), "bytes")
        else:
            print("将写:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
