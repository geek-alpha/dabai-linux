#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 data/tool_spill 里的「读取型产物」残留（落盘套娃的燃料）。

判据复用 memory._is_readback_text：正文每行带 `   123│ ` 行号前缀。
这类文件是 read_lines/code_read 把已落盘正文读回来后的产物——它们本就不该再落盘
（指针写着「用 read_lines 读它」，读回来又变成新指针，判据一个字节进不了上下文）。
历史遗留的这批是死重，清掉；同时从 index.jsonl 移除指向它们的条目（死指针比没指针更坏）。

默认 dry-run，只打印清单；--apply 才真删。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory  # noqa: E402

SPILL_DIR = ROOT / "data" / "tool_spill"


def scan() -> tuple[list[Path], int]:
    victims, total = [], 0
    for p in sorted(SPILL_DIR.glob("*.txt")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if memory._is_readback_text(text):
            victims.append(p)
            total += p.stat().st_size
    return victims, total


def drop_index_entries(names: set) -> int:
    idx = SPILL_DIR / "index.jsonl"
    if not idx.exists() or not names:
        return 0
    keep, dropped = [], 0
    for line in idx.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("file") in names:
                dropped += 1
                continue
        except Exception:
            continue
        keep.append(line)
    tmp = idx.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    tmp.replace(idx)
    return dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删；默认只列清单")
    args = ap.parse_args()

    victims, total = scan()
    all_files = [p for p in SPILL_DIR.glob("*.txt")]
    print(f"落盘文件 {len(all_files)} 个 / {sum(p.stat().st_size for p in all_files) / 1024:.0f}KB")
    print(f"读取型产物 {len(victims)} 个 / {total / 1024:.0f}KB")
    for p in victims[:3]:
        print(f"  例：{p.name} {p.stat().st_size}B")
    if not args.apply:
        print("（dry-run，未删任何东西；加 --apply 执行）")
        return 0

    names, freed = set(), 0
    for p in victims:
        try:
            freed += p.stat().st_size
            p.unlink()
            names.add(p.name)
        except OSError as e:
            print(f"删除失败 {p.name}: {e}")
    idx_dropped = drop_index_entries(names)
    left = [p for p in SPILL_DIR.glob("*.txt")]
    print(f"已删 {len(names)} 个 / {freed / 1024:.0f}KB，索引同步移除 {idx_dropped} 条")
    print(f"剩余落盘文件 {len(left)} 个 / {sum(p.stat().st_size for p in left) / 1024:.0f}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
