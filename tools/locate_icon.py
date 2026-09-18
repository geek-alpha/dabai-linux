#!/usr/bin/env python3
"""在真机截图里定位 App 图标，并判定它到底是哪一版图标。

为什么需要：launcher 图标是自绘的，uiautomator 读不到控件文字，只能看图。
直接全屏 × 全尺寸扫模板太慢（Pi 3 上 300s 都跑不完），所以先用「深色圆角方块」这个
图标底色特征把候选区域圈出来（纯 numpy 掩膜 + 连通块，毫秒级），再在候选 ROI 里扫尺寸。

用法：
  python tools/locate_icon.py --shot data/android/shot-head.png \
      --ref web/icons/icon-1024.png --ref /tmp/v1-old.png --out web/head-applied.png
"""
from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np

SIZES = range(64, 300, 2)


def dark_squares(bgr: np.ndarray, min_area: int = 4000) -> list:
    """按图标底色（深蓝黑圆角方块）找候选区域，返回 [(area, x, y, w, h)] 降序。"""
    a = bgr.astype(int)
    b, g, r = a[..., 0], a[..., 1], a[..., 2]
    m = ((r < 45) & (g < 55) & (b > 20) & (b < 90)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= min_area and 0.6 <= w / max(h, 1) <= 1.7:
            out.append((int(area), int(x), int(y), int(w), int(h)))
    return sorted(out, reverse=True)


def match_in_roi(gray: np.ndarray, roi: tuple, refs: dict) -> dict:
    """在 ROI 内扫尺寸做模板匹配，返回 {名字: (分数, 尺寸, 左上角)}。"""
    x0, y0, x1, y1 = roi
    sub = gray[y0:y1, x0:x1]
    res = {}
    for name, img in refs.items():
        best = (0.0, 0, 0, 0)
        for s in SIZES:
            t = cv2.resize(img, (s, s), interpolation=cv2.INTER_AREA)
            if t.shape[0] >= sub.shape[0] or t.shape[1] >= sub.shape[1]:
                continue
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx > best[0]:
                best = (float(mx), s, x0 + loc[0], y0 + loc[1])
        res[name] = best
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", required=True)
    ap.add_argument("--ref", action="append", required=True, help="参照图（可多次）")
    ap.add_argument("--out", default="")
    ap.add_argument("--pad", type=int, default=24)
    ap.add_argument("--roi", default="", help="直接给定 x0,y0,x1,y1，跳过深色方块启发式")
    a = ap.parse_args()

    bgr = cv2.imread(a.shot, cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if a.roi:
        x0, y0, x1, y1 = [int(t) for t in a.roi.split(",")]
        roi = (x0, y0, x1, y1)
        print(f"ROI 指定: {roi}")
    else:
        cands = dark_squares(bgr)
        if not cands:
            print("没找到候选图标区域")
            return 1
        area, x, y, w, h = cands[0]
        print(f"候选区域 #{len(cands)} 最大: {w}x{h} @ ({x},{y}) 面积 {area}")
        for c in cands[1:4]:
            print(f"  次选: {c[3]}x{c[4]} @ ({c[1]},{c[2]}) 面积 {c[0]}")
        roi = (max(0, x - a.pad), max(0, y - a.pad),
               min(gray.shape[1], x + w + a.pad), min(gray.shape[0], y + h + a.pad))
    refs = {}
    for i, p in enumerate(a.ref):
        refs[f"ref{i}:{p.split('/')[-1]}"] = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    res = match_in_roi(gray, roi, refs)
    order = sorted(res.items(), key=lambda kv: -kv[1][0])
    for name, (sc, s, px, py) in order:
        print(f"{name}: 匹配 {sc:.4f} @ {s}px ({px},{py})")
    if len(order) > 1:
        print("分差(冠军-亚军):", round(order[0][1][0] - order[1][1][0], 4))

    sc, s, px, py = order[0][1]
    for name, (sc2, s2, px2, py2) in res.items():
        d = np.abs(gray[py2:py2 + s2, px2:px2 + s2].astype(int)
                   - cv2.resize(refs[name], (s2, s2), interpolation=cv2.INTER_AREA).astype(int))
        print(f"{name}: 裁片 MAE {d.mean():.2f}")

    if a.out:
        tiles = [cv2.resize(cv2.imread(p, cv2.IMREAD_COLOR), (320, 320), interpolation=cv2.INTER_AREA)
                 for p in a.ref]
        tiles.insert(1, cv2.resize(bgr[py:py + s, px:px + s], (320, 320), interpolation=cv2.INTER_NEAREST))
        sep = np.full((320, 12, 3), 40, np.uint8)
        row = np.hstack([x for t in tiles for x in (t, sep)][:-1])
        cv2.imwrite(a.out, row)
        print("写出:", a.out, "（顺序：参照图 / 真机裁片 / 其余参照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
