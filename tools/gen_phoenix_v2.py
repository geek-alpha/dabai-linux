#!/usr/bin/env python3
"""白头凤 App 图标 v2：一形两读（远看严肃凤头 / 近看可爱小凤凰）。

第一性原理：
  远看（≤32px）只有外轮廓与大色块起作用 → 外轮廓必须是「凤头」语义：
  冠羽上扬、头部饱满、喙部锐利，左右对称给出庄严感。
  近看（≥192px）细节才生效 → 用负空间挖出圆眼，头身比做大、线条全圆曲，
  读作一只圆滚滚的小凤凰。可爱来自「大头 + 圆眼 + 短圆身」。

构造管线（四个变体共用）：
  控制点 → Catmull-Rom 平滑（线条流畅）→ 3x 超采样栅格化 → 减掉挖孔 → 渐变填充

用法：
  python tools/gen_phoenix_v2.py --ascii            # 终端并排预览四个变体
  python tools/gen_phoenix_v2.py --sheet <png>      # 出对比大图（含远看模拟）
  python tools/gen_phoenix_v2.py --one v2 --out x.png
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

S = 1024
SS = 3
CX = S / 2

BG_TOP = (8, 12, 26)
BG_BOT = (21, 30, 56)
FG_TOP = (255, 255, 255)
FG_BOT = (110, 224, 255)


def _mirror(p):
    return (S - p[0], p[1])


def cr(pts, closed=False, samples=18):
    """Catmull-Rom 样条：过所有控制点，线条自然流畅。samples=1 时退化为直线。"""
    p = [np.asarray(q, dtype=float) for q in pts]
    n = len(p)
    if samples <= 1 or n < 3:
        return [tuple(q) for q in p]
    out = []
    rng = range(n) if closed else range(n - 1)
    for i in rng:
        p0 = p[(i - 1) % n] if closed else p[max(i - 1, 0)]
        p1 = p[i]
        p2 = p[(i + 1) % n] if closed else p[i + 1]
        p3 = p[(i + 2) % n] if closed else p[min(i + 2, n - 1)]
        t = np.linspace(0.0, 1.0, samples, endpoint=False)[:, None]
        t2, t3 = t * t, t * t * t
        q = (0.5 * ((2 * p1) + (-p0 + p2) * t
                    + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                    + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
        out += [tuple(v) for v in q]
    if not closed:
        out.append(tuple(p[-1]))
    return out


# ── 四个变体 ────────────────────────────────────────────────────
# half: 左半控制点（首尾落在对称轴上，自动镜像成完整轮廓）
# full: 完整控制点（侧视等非对称造型）
# holes: 挖孔圆 (cx, cy, r)，负空间用
# smooth: 平滑采样数（1 = 保留锐角直线）

V1 = dict(
    name="V1 圆冠",
    note="正面小凤凰，圆头大眼，冠羽三尖上扬",
    half=[
        (512, 56),    # 中冠羽尖
        (494, 150),   # 谷（浅）
        (444, 92),    # 左冠羽尖（外扬）
        (450, 196),   # 谷
        (388, 150),   # 第三冠羽尖
        (434, 242),   # 冠羽根
        (372, 270),   # 头顶
        (334, 342),   # 头最宽（大圆头 = 可爱）
        (338, 434),   # 头下
        (376, 496),   # 脸颊
        (412, 530),   # 颈
        (400, 568),
        (356, 622),   # 翅鼓
        (392, 692),   # 翅尖
        (452, 738),
        (486, 788),
        (512, 828),   # 尾底
    ],
    holes=[(434, 400, 38)],
    smooth=18,
)

V2 = dict(
    name="V2 侧影",
    note="侧视严肃凤头，冠羽后飘，尖喙，圆眼",
    full=[
        (176, 232),   # 冠羽第1尖（最远）
        (252, 312),   # 谷（深，让尖羽分离）
        (320, 118),   # 冠羽第2尖
        (392, 288),   # 谷
        (452, 128),   # 冠羽第3尖
        (486, 252),   # 冠羽根 / 头顶
        (540, 232),   # 额头
        (612, 258),   # 喙上缘
        (706, 300),   # 喙尖
        (616, 342),   # 喙下缘
        (566, 398),   # 下颌
        (556, 470),   # 颈前
        (600, 548),   # 胸
        (586, 640),   # 腹前
        (636, 716),   # 尾尖
        (540, 764),
        (444, 748),
        (372, 700),
        (304, 620),
        (262, 520),
        (240, 420),   # 背
        (226, 310),   # 后颈
    ],
    holes=[(508, 292, 40)],
    smooth=18,
)

V3 = dict(
    name="V3 火羽",
    note="极简一笔，冠羽连成火焰，无眼纯剪影",
    half=[
        (512, 62),    # 火焰主尖
        (488, 158),
        (440, 102),   # 火舌二
        (452, 210),
        (392, 164),   # 火舌三
        (432, 254),
        (368, 282),
        (330, 358),   # 头最宽
        (338, 450),
        (378, 508),
        (408, 542),   # 颈
        (394, 584),
        (348, 638),   # 翅鼓
        (388, 708),
        (452, 750),
        (512, 824),
    ],
    holes=[],
    smooth=22,
)

V4 = dict(
    name="V4 锐冠",
    note="科幻几何，锐角冠羽，尖喙，小圆眼",
    half=[
        (512, 66),    # 中冠尖
        (492, 168),
        (438, 92),    # 左冠尖
        (448, 216),
        (382, 154),   # 第三冠尖
        (428, 262),
        (370, 302),
        (330, 382),
        (338, 472),
        (384, 522),
        (404, 562),
        (348, 618),
        (386, 702),
        (452, 750),
        (512, 820),
    ],
    holes=[(440, 402, 34)],
    smooth=1,          # 保留锐角
)

VARIANTS = {v["name"].split()[0].lower(): v for v in (V1, V2, V3, V4)}


def outline(v) -> list:
    """返回完整闭合轮廓点列。"""
    if "full" in v:
        return cr(v["full"], closed=True, samples=v["smooth"])
    half = v["half"]
    left = cr(half, closed=False, samples=v["smooth"])
    right = [_mirror(p) for p in reversed(left[1:-1])]
    return left + right


def holes_of(v) -> list:
    """挖孔圆列表（含镜像）。"""
    out = []
    for (cx, cy, r) in v.get("holes", []):
        out.append((cx, cy, r))
        if "full" not in v and abs(cx - CX) > 1:
            out.append((S - cx, cy, r))
    return out


def mask(v, size: int = S, ss: int = SS) -> Image.Image:
    big = size * ss
    m = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(m)
    k = big / S
    d.polygon([(x * k, y * k) for x, y in outline(v)], fill=255)
    for (cx, cy, r) in holes_of(v):
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=0)
    return m.resize((size, size), Image.LANCZOS)


def _grad(size, top, bot):
    y = np.linspace(0.0, 1.0, size)[:, None]
    x = np.linspace(-0.18, 0.18, size)[None, :]
    t = np.clip(y + x, 0.0, 1.0)
    a, b = np.array(top, float), np.array(bot, float)
    arr = a[None, None, :] * (1 - t[..., None]) + b[None, None, :] * t[..., None]
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def render(v, size: int = S, pad: float = 1.0, bg: bool = True) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if bg:
        base.paste(_grad(size, BG_TOP, BG_BOT).convert("RGBA"), (0, 0))
    m = mask(v, size)
    if pad != 1.0:
        inner = max(1, int(size * pad))
        small = m.resize((inner, inner), Image.LANCZOS)
        m = Image.new("L", (size, size), 0)
        off = (size - inner) // 2
        m.paste(small, (off, off))
    base.paste(_grad(size, FG_TOP, FG_BOT).convert("RGBA"), (0, 0), m)
    return base


def ascii_one(v, width=58) -> list:
    h = max(8, int(width * 0.5))
    m = mask(v, 256).resize((width, h), Image.LANCZOS)
    a = np.asarray(m, dtype=float) / 255.0
    ramp = " .:-=+*#%@"
    return ["".join(ramp[min(len(ramp) - 1, int(t * len(ramp)))] for t in row)
            for row in a]


def ascii_all(width=58) -> str:
    blocks = {k: ascii_one(v, width) for k, v in VARIANTS.items()}
    heads = list(VARIANTS)
    lines = ["  ".join(f"{h:<{width}}" for h in heads)]
    lines += ["  ".join(f"{v['note'][:width]:<{width}}" for v in VARIANTS.values())]
    for i in range(len(next(iter(blocks.values())))):
        lines.append("  ".join(blocks[h][i] for h in heads))
    return "\n".join(lines)


def sheet(path: str, cell=300) -> str:
    """对比大图：上排大图（近看），下排 32px 放大（远看模拟）。"""
    keys = list(VARIANTS)
    pad, gap, label = 16, 18, 34
    W = pad * 2 + len(keys) * cell + (len(keys) - 1) * gap
    H = pad * 2 + label + cell + gap + label + cell
    out = Image.new("RGB", (W, H), (12, 14, 22))
    d = ImageDraw.Draw(out)
    for i, k in enumerate(keys):
        v = VARIANTS[k]
        x = pad + i * (cell + gap)
        big = render(v, cell)
        out.paste(big, (x, pad + label))
        # 远看模拟：先缩到 32px 再放大回 cell，模拟真机小图标
        tiny = render(v, 32).resize((cell, cell), Image.NEAREST)
        y2 = pad + label + cell + gap + label
        out.paste(tiny, (x, y2))
        d.text((x, pad + 8), f"{v['name']}  {v['note']}", fill=(190, 200, 220))
        d.text((x, y2 - 24), "远看模拟 (32px → 放大)", fill=(120, 132, 156))
    out.save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ascii", action="store_true")
    ap.add_argument("--width", type=int, default=58)
    ap.add_argument("--sheet", default="")
    ap.add_argument("--one", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if a.sheet:
        print("对比图:", sheet(a.sheet))
    if a.one:
        v = VARIANTS[a.one.lower()]
        out = a.out or f"{a.one}.png"
        render(v, 1024).save(out)
        print("写出:", out)
    if a.ascii or not (a.sheet or a.one):
        print(ascii_all(a.width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
