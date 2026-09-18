#!/usr/bin/env python3
"""白头凤 App 图标：V1 头部特写（只留冠羽 + 圆头 + 圆眼，居中放大）。

第一性原理：图标在 48px 下能读出的只有外轮廓和一两个大色块。整只凤凰缩到 48px 时，
冠羽、翅膀、爪子会糊成一团互相抵消；只留头部这一块最识别的形状并放大，远看仍是凤头，
近看更干净——少即是多。

几何：取 gen_phoenix_v2.V1 的冠羽到颈部控制点，底部补一段圆收口（避免平切一刀的断面感），
再按「实际栅格化出的墨迹 bbox」居中缩放（不用控制点估 bbox——Catmull-Rom 会过冲）。

用法：
  python tools/gen_phoenix_head.py --ascii                    # 终端预览
  python tools/gen_phoenix_head.py --preview web/head.png     # 出对比图（左全身 V1 / 右头部特写）
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_phoenix_v2 as P  # noqa: E402

S = P.S
CX = P.S / 2
FILL = 0.86          # 墨迹长边占画布比例，四周各留 7% 白边

_HALF = [
    (512, 56),    # 中冠羽尖
    (494, 150),   # 谷
    (444, 92),    # 左冠羽尖
    (450, 196),   # 谷
    (388, 150),   # 第三冠羽尖
    (434, 242),   # 冠羽根
    (372, 270),   # 头顶
    (334, 342),   # 头最宽
    (338, 434),   # 头下
    (376, 496),   # 脸颊
    (430, 552),   # 下巴
    (512, 566),   # 底部中点（落在对称轴上，收口成圆弧）
]
_EYES = [(434, 400, 38)]


def _fit(half, holes, frac=FILL):
    """缩放平移，使栅格化墨迹的 bbox 居中、长边占 frac 画布。"""
    probe = dict(half=half, holes=holes, smooth=P.V1["smooth"])
    ys, xs = np.nonzero(np.asarray(P.mask(probe, S)))
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    k = frac * S / max(x1 - x0 + 1, y1 - y0 + 1)
    cx, cy = (x0 + x1 + 1) / 2, (y0 + y1 + 1) / 2

    def pt(p):
        return (round((p[0] - cx) * k + CX, 2), round((p[1] - cy) * k + CX, 2))

    new_holes = []
    for hx, hy, r in holes:
        x, y = pt((hx, hy))
        new_holes.append((x, y, round(r * k, 2)))
    return [pt(p) for p in half], new_holes


_HALF, _HOLES = _fit(_HALF, _EYES)

V1 = dict(
    name="V1 头",
    note="V1 头部特写：冠羽 + 圆头 + 圆眼，居中放大",
    half=_HALF,
    holes=_HOLES,
    smooth=P.V1["smooth"],
)

VARIANTS = {"v1": V1}

# 复用 v2 的渲染管线（几何空间同为 1024，SVG 走 base.outline 也一致）
cr = P.cr
outline = P.outline
holes_of = P.holes_of
mask = P.mask
render = P.render
BG_TOP, BG_BOT, FG_TOP, FG_BOT, SS = P.BG_TOP, P.BG_BOT, P.FG_TOP, P.FG_BOT, P.SS


def ink_bbox(v, size: int = S):
    """实际墨迹 bbox（含挖孔）——验证居中用。"""
    ys, xs = np.nonzero(np.asarray(mask(v, size)))
    return int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())


def preview(path: str, cell: int = 420) -> str:
    """左：V1 全身；右：头部特写。附 48px 远看模拟。"""
    items = [("V1 全身 (上一版)", P.V1), ("V1 头 居中", V1)]
    gap, label, small = 20, 34, 48
    W = gap * (len(items) + 1) + cell * len(items)
    H = gap + label + cell + gap + label + small + gap
    c = Image.new("RGB", (W, H), (16, 20, 32))
    d = ImageDraw.Draw(c)
    for i, (lab, v) in enumerate(items):
        x = gap + i * (cell + gap)
        d.text((x, gap - 16), lab, fill=(200, 220, 255))
        c.paste(render(v, cell), (x, gap + label - 16))
        sm = render(v, small)
        c.paste(sm, (x, gap + label + cell + gap + label - 30))
        d.text((x + small + 10, gap + label + cell + gap + label - 18),
               "48px 远看", fill=(140, 170, 210))
    c.save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ascii", action="store_true")
    ap.add_argument("--preview", default="")
    ap.add_argument("--bbox", action="store_true")
    a = ap.parse_args()
    if a.ascii:
        print("\n".join(P.ascii_one(V1, 64)))
    if a.bbox:
        for size in (1024, 192, 48):
            x0, x1, y0, y1 = ink_bbox(V1, size)
            print(f"{size:>4}px 墨迹 x[{x0},{x1}] y[{y0},{y1}] "
                  f"边距 上{y0} 下{size - 1 - y1} 左{x0} 右{size - 1 - x1}")
    if a.preview:
        print("写出:", preview(a.preview))
    return 0


if __name__ == "__main__":
    sys.exit(main())
