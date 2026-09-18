#!/usr/bin/env python3
"""最早那版展翅凤凰（gen_phoenix_icon）的适配层。

apply_phoenix_icon 统一按 v2 接口调用模块（VARIANTS / outline(v) / holes_of / shapes_of /
mask(v,size) / render(v,size,pad,bg)），而旧版是硬编码单形状、没有变体参数。
这里把它包成同一套接口，铺图管线共用一条，不再为它单写一份脚本。
"""
from __future__ import annotations

import gen_phoenix_icon as G

S = G.S
BG_TOP, BG_BOT = G.BG_TOP, G.BG_BOT
FG_TOP, FG_BOT = G.FG_TOP, G.FG_BOT

VARIANTS = {
    "l1": dict(name="L1 展翅", note="最早那版展翅凤凰剪影（09-14 22:14 预览的形状）"),
}


def outline(v) -> list:
    return G.outline()


def holes_of(v) -> list:
    return []


def shapes_of(v, key: str) -> list:
    return []


def cr(pts, closed: bool = True, samples: int = 1) -> list:
    """SVG 用：旧版轮廓本身就是贝塞尔采样点（每段 64 点），无需再平滑。"""
    return list(pts)


def mask(v, size: int = S, ss: int = G.SS):
    return G.mask(size, ss)


def render(v, size: int = S, pad: float = 1.0, bg: bool = True):
    return G.render(size, pad=pad, bg=bg)
