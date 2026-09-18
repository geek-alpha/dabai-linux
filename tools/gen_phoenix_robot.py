#!/usr/bin/env python3
"""白头凤 App 图标 v3：机器人化 —— 圆润几何 + 机器人识别符号。

第一性原理：
  远看（≤32px）只有外轮廓起作用 → 机器人最强的远看符号是「天线 + 圆头」；
  圆润几何（大圆角、零锐角）给出「可爱」，与「白头凤」的冠羽语义合并成天线。
  近看（≥192px）细节生效 → 负空间挖双圆眼 + 小横条嘴，读作机器人面孔。

与 v2（凤凰造型）共用平滑与栅格化管线：
  控制点 → Catmull-Rom 平滑 → 3x 超采样 → 加形状(adds) → 减形状(holes) → 渐变填充
本版新增 circle / rrect 两类形状，用来画天线、侧耳、面罩。

用法：
  python tools/gen_phoenix_robot.py --ascii                  # 终端并排预览
  python tools/gen_phoenix_robot.py --sheet <png>            # 对比大图（含 32px 远看模拟）
  python tools/gen_phoenix_robot.py --one r1 --out x.png     # 单个出图
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_phoenix_v2 as P  # noqa: E402

S, SS, CX = P.S, P.SS, P.CX

# ── 科幻发光层 ─────────────────────────────────────────────────
GLOW = (232, 250, 255)   # 光条亮芯（近白）
HALO = (110, 224, 255)    # 能量青：外发光与辉光晕

# ── 四个机器人变体 ──────────────────────────────────────────────
# half: 左半控制点（首尾落在对称轴上，自动镜像）
# adds: 叠加形状（天线、侧耳），先于 holes 绘制
# holes: 挖孔形状（眼睛、面罩）
# 形状语法：("circle", cx, cy, r) / ("rrect", x1, y1, x2, y2, radius)

R1 = dict(
    name="R1 三羽天线",
    note="圆头小机器人，三根圆头天线（冠羽的机器人化）",
    note_en="round head + 3 crest antennas",
    half=[
        (512, 300),   # 头顶正中
        (430, 306),
        (384, 358),   # 头左上圆角
        (362, 448),   # 头最宽
        (374, 538),
        (414, 594),   # 下颌
        (450, 632),   # 颈
        (438, 678),
        (394, 722),   # 身左
        (380, 794),
        (432, 852),   # 底座圆角
        (512, 868),
    ],
    adds=[
        ("rrect", 500, 176, 524, 312, 12),   # 中天线杆
        ("circle", 512, 164, 30),            # 中天线球
        ("rrect", 442, 214, 464, 322, 11),   # 左天线杆
        ("circle", 453, 202, 26),            # 左天线球
    ],
    holes=[
        ("circle", 434, 448, 46),            # 眼
        ("circle", 512, 548, 22),            # 小圆嘴
    ],
    smooth=20,
)

R2 = dict(
    name="R2 单天线正圆",
    note="最圆的头 + 一根长天线 + 最大圆眼，可爱度最高",
    note_en="roundest head + 1 long antenna",
    half=[
        (512, 286),
        (420, 294),
        (366, 356),
        (344, 452),   # 头最宽（近正圆）
        (360, 548),
        (408, 606),
        (452, 646),   # 颈
        (444, 690),
        (400, 730),
        (386, 800),
        (436, 854),
        (512, 870),
    ],
    adds=[
        ("rrect", 502, 140, 522, 300, 10),   # 长天线杆
        ("circle", 512, 126, 30),            # 天线球
    ],
    holes=[
        ("circle", 430, 442, 52),            # 大圆眼
        ("rrect", 490, 528, 534, 548, 10),   # 横条嘴
    ],
    smooth=20,
)

R3 = dict(
    name="R3 圆冠面罩",
    note="圆润冠羽（保留凤凰语义）+ 横贯面罩，最像机器人脸",
    note_en="soft crest + full-width visor",
    half=[
        (512, 104),   # 中冠圆顶
        (474, 148),
        (436, 130),   # 左冠圆顶
        (404, 180),
        (386, 240),   # 冠根
        (362, 306),
        (348, 404),   # 头最宽
        (358, 502),
        (392, 566),
        (434, 610),   # 下颌
        (452, 656),
        (420, 716),
        (400, 796),
        (444, 856),
        (512, 872),
    ],
    adds=[
        # 三角翼（平展）：根上 → 翼尖 → 根下，三点 Catmull-Rom 平滑出圆润三角
        ("poly", [(404, 604), (140, 636), (418, 768)], 7),
        # 副翼（手的小翅膀化）：细长三角，与大翼同向、同扫掠，翼尖朝外下
        ("poly", [(420, 782), (276, 792), (446, 826)], 6),
    ],
    holes=[
        ("circle", 436, 430, 46),            # 眼
        ("rrect", 452, 528, 572, 550, 11),   # 横贯面罩
    ],
    # 科幻发光层：双圆眼改成「发光透镜」（比洞口小 → 留一圈暗环当眼窝），副翼内芯发光刃口
    glow=[
        ("circle", 436, 430, 36),
        ("poly", [(414, 787), (299, 801), (437, 821)], 6),
    ],
    halo=True,   # 剪影外一圈青色外发光
    # 鸟爪：随主体一起缩放（坐标写 1024 系）
    paws=[
        ("rrect", 446, 806, 490, 884, 16),                    # 腿（顶部埋进底座）
        ("poly", [(448, 858), (476, 878), (380, 924)], 7),    # 外趾
        ("poly", [(456, 882), (494, 882), (472, 958)], 7),    # 中趾
    ],
    scale=0.88,
    smooth=20,
)

R4 = dict(
    name="R4 方头侧耳",
    note="圆角方头 + 两侧圆耳（耳机感）+ 小横条嘴，最硬朗",
    note_en="rounded square head + side ears",
    half=[
        (512, 258),   # 头顶中
        (450, 264),
        (400, 288),   # 左上圆角
        (378, 344),
        (372, 416),   # 头左侧
        (378, 488),
        (398, 540),   # 左下圆角
        (430, 566),
        (452, 592),   # 颈（收窄）
        (456, 640),
        (432, 690),   # 肩外扩
        (410, 760),
        (408, 830),
        (448, 866),
        (512, 880),
    ],
    adds=[
        ("circle", 368, 416, 44),            # 左耳
    ],
    holes=[
        ("circle", 436, 396, 42),            # 眼
        ("rrect", 480, 486, 544, 504, 9),    # 横条嘴
    ],
    smooth=22,
)

VARIANTS = {v["name"].split()[0].lower(): v for v in (R1, R2, R3, R4)}


def mirror_shape(sh):
    """形状沿对称轴镜像；正好骑在轴上时返回自身（调用方据此去重）。"""
    if sh[0] == "circle":
        _, cx, cy, r = sh
        return ("circle", S - cx, cy, r)
    if sh[0] == "rrect":
        _, x1, y1, x2, y2, r = sh
        return ("rrect", S - x2, y1, S - x1, y2, r)
    if sh[0] == "poly":
        _, pts, sm = sh
        return ("poly", [(S - x, y) for x, y in pts], sm)
    raise ValueError(f"未知形状: {sh[0]}")


def draw_shape(d, sh, k, fill):
    if sh[0] == "circle":
        _, cx, cy, r = sh
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)
    elif sh[0] == "rrect":
        _, x1, y1, x2, y2, r = sh
        d.rounded_rectangle([x1 * k, y1 * k, x2 * k, y2 * k], radius=r * k, fill=fill)
    elif sh[0] == "poly":
        _, pts, sm = sh
        d.polygon([(x * k, y * k) for x, y in P.cr(pts, closed=True, samples=sm)], fill=fill)
    else:
        raise ValueError(f"未知形状: {sh[0]}")


def _span_ys(v) -> list:
    """half/adds/paws 的垂直范围，用来定整体缩放中心。"""
    ys = [y for _, y in v["half"]]
    for key in ("adds", "paws", "glow"):
        for sh in v.get(key, []):
            if sh[0] == "circle":
                ys += [sh[2] - sh[3], sh[2] + sh[3]]
            elif sh[0] == "rrect":
                ys += [sh[2], sh[4]]
            elif sh[0] == "poly":
                ys += [y for _, y in sh[1]]
    return ys


def fit(v, k=None):
    """整体缩放：绕对称轴与整体垂直中心收缩，half/adds/paws 一并缩，比例不变。"""
    k = v.get("scale", 1.0) if k is None else k
    if k == 1.0:
        return v
    ys = _span_ys(v)
    cy = (min(ys) + max(ys)) / 2

    def pt(x, y):
        return (CX + (x - CX) * k, cy + (y - cy) * k)

    def sc(sh):
        if sh[0] == "circle":
            _, x, y, r = sh
            x, y = pt(x, y)
            return ("circle", x, y, r * k)
        if sh[0] == "rrect":
            _, x1, y1, x2, y2, r = sh
            x1, y1 = pt(x1, y1)
            x2, y2 = pt(x2, y2)
            return ("rrect", x1, y1, x2, y2, r * k)
        if sh[0] == "poly":
            _, pts, sm = sh
            return ("poly", [pt(x, y) for x, y in pts], sm)
        raise ValueError(f"未知形状: {sh[0]}")

    out = dict(v)
    out["half"] = [pt(x, y) for x, y in v["half"]]
    out["adds"] = [sc(s) for s in v.get("adds", [])]
    out["holes"] = [sc(s) for s in v.get("holes", [])]
    out["glow"] = [sc(s) for s in v.get("glow", [])]
    out["paws"] = [sc(s) for s in v.get("paws", [])]
    return out


def shapes_of(v, key) -> list:
    out = []
    for sh in v.get(key, []):
        out.append(sh)
        if "full" not in v:
            m = mirror_shape(sh)
            if m != sh:
                out.append(m)
    return out


def mask(v, size: int = S, ss: int = SS) -> Image.Image:
    big = size * ss
    m = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(m)
    k = big / S
    fv = fit(v)
    d.polygon([(x * k, y * k) for x, y in P.outline(fv)], fill=255)
    for sh in shapes_of(fv, "adds"):
        draw_shape(d, sh, k, 255)
    for sh in shapes_of(fv, "paws"):
        draw_shape(d, sh, k, 255)
    for sh in shapes_of(fv, "holes"):
        draw_shape(d, sh, k, 0)
    return m.resize((size, size), Image.LANCZOS)


def _inset(m: Image.Image, size: int, pad: float) -> Image.Image:
    """遮罩整体缩到画布 pad 倍并居中（自适应图标前景的安全区）。"""
    if pad == 1.0:
        return m
    inner = max(1, int(round(size * pad)))
    out = Image.new("L", (size, size), 0)
    out.paste(m.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2,) * 2)
    return out


def _bg_vertical(size: int) -> Image.Image:
    """纯竖向深空底——与 adaptive-icon 的 ic_launcher_background.xml 同色同向。"""
    t = np.linspace(0.0, 1.0, size)[:, None, None]
    top = np.array(P.BG_TOP, float)[None, None, :]
    bot = np.array(P.BG_BOT, float)[None, None, :]
    arr = np.repeat(top * (1 - t) + bot * t, size, axis=1)
    return Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")


def render(v, size: int = S, pad: float = 1.0, bg: bool = True) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if bg:
        base.paste(P._grad(size, P.BG_TOP, P.BG_BOT).convert("RGBA"), (0, 0))
    m = _inset(mask(v, size), size, pad)
    # 科幻外发光：剪影外一圈青色辉光，先贴、主体随后盖掉内侧
    if v.get("halo"):
        bloom = m.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.028)))
        alpha = bloom.point(lambda a: int(a * 0.55))
        if bg:
            base.paste(Image.new("RGBA", (size, size), HALO + (255,)), (0, 0), alpha)
        else:
            # 自适应前景层：启动器会丢掉低透明度像素（实测辉光在桌面上完全不显示），
            # 所以把辉光预先叠在深空底上烤成不透明——颜色一样，桌面就能看见
            halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            halo.paste(Image.new("RGBA", (size, size), HALO + (255,)), (0, 0), alpha)
            solid = Image.alpha_composite(_bg_vertical(size), halo)
            base = Image.composite(solid, base, alpha.point(lambda a: 255 if a >= 2 else 0))
    base.paste(P._grad(size, P.FG_TOP, P.FG_BOT).convert("RGBA"), (0, 0), m)
    # 发光部件（面罩光条、副翼刃口）：柔和辉光打底 + 近白亮芯压顶
    # 裁剪到「未挖孔」的剪影：光只能从体内发出来，不会飘到体外；面罩洞口也被光填满
    if v.get("glow"):
        vf = dict(v)
        vf["holes"] = []
        mf = _inset(mask(vf, size), size, pad)
        for sh in shapes_of(fit(v), "glow"):
            lay = Image.new("L", (size, size), 0)
            draw_shape(ImageDraw.Draw(lay), sh, size / S, 255)
            lay = ImageChops.multiply(_inset(lay, size, pad), mf)
            soft = lay.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.014)))
            base.paste(Image.new("RGBA", (size, size), HALO + (255,)), (0, 0),
                       soft.point(lambda a: int(a * 0.85)))
            base.paste(Image.new("RGBA", (size, size), GLOW + (255,)), (0, 0), lay)
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


def symmetry_error(v, size=512) -> float:
    """左右镜像平均像素差，0 = 完美对称（形状是否走偏的自检指标）。"""
    m = np.asarray(mask(v, size), dtype=float) / 255.0
    return round(float(np.abs(m - m[:, ::-1]).mean()), 6)


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
        out.paste(render(v, cell), (x, pad + label))
        tiny = render(v, 32).resize((cell, cell), Image.NEAREST)
        y2 = pad + label + cell + gap + label
        out.paste(tiny, (x, y2))
        d.text((x, pad + 8), f"{v['name'].split()[0]}  {v['note_en']}", fill=(190, 200, 220))
        d.text((x, y2 - 24), f"32px far view      symmetry diff {symmetry_error(v)}", fill=(120, 132, 156))
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
