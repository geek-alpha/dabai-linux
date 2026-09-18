#!/usr/bin/env python3
"""白头凤 App 图标生成器：抽象凤凰（鸟类）+ 深空切角（科幻）。

设计约束（对齐 Apple logo 的抽象度）：
  单一剪影、左右严格对称、无内部细节、留白 ~30%、一处锐利记忆点（翼尖切角）。
形状全部由三次贝塞尔定义，3x 超采样后 LANCZOS 缩小得到抗锯齿边缘。

用法：
  python tools/gen_phoenix_icon.py --ascii          # 终端预览形状（自检）
  python tools/gen_phoenix_icon.py --out <dir>      # 输出全套 PNG
  python tools/gen_phoenix_icon.py --svg <file>     # 输出矢量源
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

S = 1024          # 逻辑画布边长
SS = 3            # 超采样倍数
CX = S / 2        # 对称轴

# ── 调色：深空底 + 白头凤（白→冰青） ──────────────────────────────
BG_TOP = (8, 12, 26)        # #080C1A 深空
BG_BOT = (21, 30, 56)       # #151E38 稍亮的夜蓝
FG_TOP = (255, 255, 255)    # 头顶纯白 = 「白头」
FG_BOT = (110, 224, 255)    # 翼尖冰青 = 科技感


def _mirror(p):
    return (S - p[0], p[1])


def bez(p0, p1, p2, p3, n=64):
    """三次贝塞尔采样，返回点列（不含起点，含终点）。"""
    t = np.linspace(0.0, 1.0, n)[:, None]
    p0, p1, p2, p3 = (np.asarray(p, dtype=float) for p in (p0, p1, p2, p3))
    pts = ((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1
           + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3)
    return [tuple(p) for p in pts]


# ── 形状：展翅上升的凤凰剪影 ──────────────────────────────────────
# 顶点（左侧，右侧由镜像生成）。翼根窄、中段鼓、翼尖锐 = 羽毛/火舌，而非披风
B = (512, 908)      # 底尖（尾）
A1 = (458, 520)     # 翼下缘 / 身体交点
A2 = (470, 470)     # 翼上缘 / 身体交点
WL = (112, 330)     # 左翼尖
WL2 = (172, 276)    # 左翼尖切角（科幻锐切）
H = (512, 180)      # 头顶（冠尖）

LO1, LO2 = (300, 600), (150, 440)   # 翼下缘：中段下鼓
UP1, UP2 = (250, 390), (370, 470)   # 翼上缘：略下凸
NE1, NE2 = (456, 360), (480, 250)   # 身体上段（颈 → 头）
TA1, TA2 = (490, 800), (462, 660)   # 身体下段（尾）


def outline() -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = [B]
    # 身体下段：底尖 → 翼根（收窄成尾）
    pts += bez(B, TA1, TA2, A1)
    # 左翼下缘：翼根 → 翼尖（中段下鼓）
    pts += bez(A1, LO1, LO2, WL)
    # 翼尖切角（直线，制造锐利记忆点）
    pts += [WL2]
    # 左翼上缘：翼尖 → 颈根
    pts += bez(WL2, UP1, UP2, A2)
    # 身体上段：颈根 → 头顶
    pts += bez(A2, NE1, NE2, H)
    # ── 右侧镜像 ──
    pts += bez(H, _mirror(NE2), _mirror(NE1), _mirror(A2))
    pts += bez(_mirror(A2), _mirror(UP2), _mirror(UP1), _mirror(WL2))
    pts += [_mirror(WL)]
    pts += bez(_mirror(WL), _mirror(LO2), _mirror(LO1), _mirror(A1))
    pts += bez(_mirror(A1), _mirror(TA2), _mirror(TA1), B)
    return pts


def mask(size: int = S, ss: int = SS) -> Image.Image:
    """形状蒙版（L 模式，255 = 标志内部）。"""
    big = size * ss
    m = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(m)
    k = big / S
    poly = [(x * k, y * k) for x, y in outline()]
    d.polygon(poly, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def _grad(size: int, top: tuple, bot: tuple) -> Image.Image:
    """垂直线性渐变（带极轻微的斜向偏移，避免死板）。"""
    y = np.linspace(0.0, 1.0, size)[:, None]
    x = np.linspace(-0.18, 0.18, size)[None, :]
    t = np.clip(y + x, 0.0, 1.0)
    top_a, bot_a = np.array(top, float), np.array(bot, float)
    arr = top_a[None, None, :] * (1 - t[..., None]) + bot_a[None, None, :] * t[..., None]
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def render(size: int = S, pad: float = 1.0, bg: bool = True) -> Image.Image:
    """渲染图标。pad<1 时把标志整体缩小（用于 maskable 安全区）。"""
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if bg:
        base.paste(_grad(size, BG_TOP, BG_BOT).convert("RGBA"), (0, 0))
    m = mask(size)
    if pad != 1.0:
        inner = max(1, int(size * pad))
        small = m.resize((inner, inner), Image.LANCZOS)
        m = Image.new("L", (size, size), 0)
        off = (size - inner) // 2
        m.paste(small, (off, off))
    fg = _grad(size, FG_TOP, FG_BOT).convert("RGBA")
    base.paste(fg, (0, 0), m)
    return base


def ascii_preview(width: int = 100) -> str:
    """终端 ASCII 预览：用来肉眼核对形状（无图形界面时的自检手段）。"""
    h = max(8, int(width * 0.5))
    m = mask(256).resize((width, h), Image.LANCZOS)
    a = np.asarray(m, dtype=float) / 255.0
    ramp = " .:-=+*#%@"
    lines = []
    for row in a:
        lines.append("".join(ramp[min(len(ramp) - 1, int(v * len(ramp)))] for v in row))
    return "\n".join(lines)


def svg() -> str:
    pts = outline()
    d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts) + " Z"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" width="{S}" height="{S}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#{BG_TOP[0]:02X}{BG_TOP[1]:02X}{BG_TOP[2]:02X}"/>
      <stop offset="1" stop-color="#{BG_BOT[0]:02X}{BG_BOT[1]:02X}{BG_BOT[2]:02X}"/>
    </linearGradient>
    <linearGradient id="fg" x1="0" y1="0" x2="0.12" y2="1">
      <stop offset="0" stop-color="#{FG_TOP[0]:02X}{FG_TOP[1]:02X}{FG_TOP[2]:02X}"/>
      <stop offset="1" stop-color="#{FG_BOT[0]:02X}{FG_BOT[1]:02X}{FG_BOT[2]:02X}"/>
    </linearGradient>
  </defs>
  <rect width="{S}" height="{S}" fill="url(#bg)"/>
  <path d="{d}" fill="url(#fg)"/>
</svg>
"""


# ── 输出尺寸 ────────────────────────────────────────────────────
WEB = {"icon-1024.png": 1024, "icon-512.png": 512, "icon-192.png": 192}
ANDROID = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# Android 8+ 自适应图标：108dp 画布，内容需落在中心 72dp 安全圆内
ADAPTIVE_FG = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
FG_SAFE = 0.85   # 标志本身占画布 ~78%，再乘 0.85 后 ≈66%，恰好落在安全圆内

ADAPTIVE_XML = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background"/>
    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>
</adaptive-icon>
"""

BG_XML = """<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <gradient android:type="linear" android:angle="270"
        android:startColor="#080C1A" android:endColor="#151E38"/>
</shape>
"""


def write_all(root: str) -> list[str]:
    written = []
    web_dir = os.path.join(root, "web", "icons")
    os.makedirs(web_dir, exist_ok=True)
    for name, sz in WEB.items():
        render(sz).save(os.path.join(web_dir, name))
        written.append(f"{web_dir}/{name}")
    # maskable：内容压到 78% 安全区，四周留底色，避免被圆形裁切吃掉翼尖
    render(512, pad=0.78).save(os.path.join(web_dir, "icon-512-maskable.png"))
    written.append(f"{web_dir}/icon-512-maskable.png")

    res = os.path.join(root, "apps", "dabai-android", "app", "src", "main", "res")
    for dens, sz in ANDROID.items():
        d = os.path.join(res, f"mipmap-{dens}")
        os.makedirs(d, exist_ok=True)
        render(sz).save(os.path.join(d, "ic_launcher.png"))
        written.append(f"{d}/ic_launcher.png")
    # 自适应图标：透明底前景 + XML 背景（启动器可任意裁切/加动效）
    for dens, sz in ADAPTIVE_FG.items():
        d = os.path.join(res, f"mipmap-{dens}")
        os.makedirs(d, exist_ok=True)
        render(sz, pad=FG_SAFE, bg=False).save(os.path.join(d, "ic_launcher_foreground.png"))
        written.append(f"{d}/ic_launcher_foreground.png")
    d = os.path.join(res, "mipmap-anydpi-v26")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "ic_launcher.xml"), "w", encoding="utf-8") as f:
        f.write(ADAPTIVE_XML)
    written.append(f"{d}/ic_launcher.xml")
    d = os.path.join(res, "drawable")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "ic_launcher_background.xml"), "w", encoding="utf-8") as f:
        f.write(BG_XML)
    written.append(f"{d}/ic_launcher_background.xml")
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ascii", action="store_true")
    ap.add_argument("--svg", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--width", type=int, default=100)
    a = ap.parse_args()
    if a.ascii:
        print(ascii_preview(a.width))
    if a.svg:
        with open(a.svg, "w", encoding="utf-8") as f:
            f.write(svg())
        print("SVG:", a.svg)
    if a.out:
        for p in write_all(a.out):
            print("写出:", p)
    if not (a.ascii or a.svg or a.out):
        print(ascii_preview(a.width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
