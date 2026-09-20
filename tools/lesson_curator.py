#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验生命周期：active → stale → archived，只归档不删除。

背景（2026-09-20）：经验库只有 FIFO 500 上限兜底，364 条全是 active——写入时刻
（ts 表）和曝光流水（data/gene_exposure.jsonl）都在手上，却没有任何东西按「这条还
活着吗」做生命周期。对照 hermes-agent/agent/curator.py:188-240 的
apply_automatic_transitions：

  · 锚点 = 最后一次真实活动时间（写入或最后一次进 prompt），不是创建时间
  · use_count == 0 是「没有证据」，不是「陈旧」——绝不据此归档（curator.py:227-232）
  · 只归档不删除，条目进 archive.json 随时可捞回
  · 阈值可配，默认 dry-run，--apply 才动

判据：
  anchor = max(写入时刻, 最后一次进 prompt 的时刻)；两者都缺 → 以「现在」为锚点
  并只做 seed（curator.py:212-214 同款）：存量条目的钟从第一次见到它开始走。
  anchor <= now - ARCHIVE_DAYS → archived（移出主库）
  anchor <= now - STALE_DAYS   → stale（仍在库、仍可被注入，只是标记为待观察）

用法：
  lesson_curator.py                 预览（不改任何文件）
  lesson_curator.py --apply         执行：写状态 + 归档到期条目
  lesson_curator.py --stale-days 30 --archive-days 90
"""
import argparse
import importlib.util
import json
import os
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "harness_task_memory.json"
ARCHIVE = BASE / "harness_task_memory.archive.json"
EXPOSURE = BASE / "data" / "gene_exposure.jsonl"
STATE = BASE / "data" / "lesson_state.json"
STALE_DAYS = 30
ARCHIVE_DAYS = 90
EXPOSURE_TAIL = 5000  # 只扫最近这些行：943 行/天量级，够覆盖任何现实锚点

_key_of = None


def key_of(text):
    """键算法唯一真源是 tools/gene_fitness.py:key_of——两份实现漂移会让埋点静默归零。"""
    global _key_of
    if _key_of is None:
        spec = importlib.util.spec_from_file_location("gene_fitness", BASE / "tools" / "gene_fitness.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _key_of = mod.key_of
    return _key_of(text)


def _load(path, default):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _save(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def last_hits(path=None, tail=EXPOSURE_TAIL) -> dict:
    """每个 lesson 键最后一次进 prompt 的时刻。坏行跳过——一条坏行不该让整个 pass 失效。

    path 在调用时解析而不是写成默认参数：默认参数在函数定义时就绑死了，改模块属性
    后它仍指向旧路径（测试里就撞过：monkeypatch 不生效，读的是真库流水）。
    """
    path = path or EXPOSURE
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-tail:]
    except Exception:
        return out
    for ln in lines:
        try:
            row = json.loads(ln)
            ts = float(row.get("ts") or 0)
            for k in row.get("keys") or []:
                if isinstance(k, str) and k.startswith("lesson:") and ts > out.get(k[7:], 0.0):
                    out[k[7:]] = ts
        except Exception:
            continue
    return out


def plan(now=None, stale_days=STALE_DAYS, archive_days=ARCHIVE_DAYS):
    """算这一轮的生命周期动作，不写任何文件。返回 (动作表, 统计)。"""
    now = float(now if now is not None else time.time())
    data = _load(FILE, {})
    lessons = [str(x) for x in (data.get("lessons") or [])]
    ts = {str(k): float(v) for k, v in (data.get("ts") or {}).items() if isinstance(v, (int, float))}
    state = _load(STATE, {})
    hits = last_hits()
    stale_cut = now - stale_days * 86400
    arch_cut = now - archive_days * 86400
    rows, counts = [], {"active": 0, "stale": 0, "archived": 0, "seeded": 0}
    for text in lessons:
        k = key_of(text)
        written = ts.get(k)
        hit = hits.get(k)
        known = [x for x in (written, hit) if x]
        if not known:
            counts["seeded"] += 1
            rows.append((k, text, "seed", None))
            continue
        anchor = max(known)
        cur = state.get(k) or "active"
        if anchor <= arch_cut:
            counts["archived"] += 1
            rows.append((k, text, "archived", anchor))
        elif anchor <= stale_cut:
            counts["stale"] += 1
            rows.append((k, text, "stale", anchor))
        else:
            counts["active"] += 1
            if cur != "active":
                rows.append((k, text, "active", anchor))
    return rows, counts


def apply(rows, now=None) -> dict:
    """执行：写状态 + 归档到期条目。归档走 tmp+replace，中途失败不留半个库。"""
    now = float(now if now is not None else time.time())
    data = _load(FILE, {})
    lessons = [str(x) for x in (data.get("lessons") or [])]
    ts = dict(data.get("ts") or {})
    state = _load(STATE, {})
    gone = {k for k, _t, act, _a in rows if act == "archived"}
    moved = [(k, t) for k, t, act, _a in rows if act == "archived"]
    if moved:
        old = _load(ARCHIVE, {})
        keep = [str(x) for x in (old.get("lessons") or [])]
        keep_ts = dict(old.get("ts") or {})
        keep.extend(t for _k, t in moved)
        keep_ts.update({k: ts.get(k) for k, _t in moved if ts.get(k) is not None})
        _save(ARCHIVE, {"lessons": keep, "ts": keep_ts})
        lessons = [t for t in lessons if key_of(t) not in gone]
        ts = {k: v for k, v in ts.items() if k not in gone}
        _save(FILE, {"lessons": lessons, "ts": ts})
    for k, _t, act, _a in rows:
        if act == "archived":
            state.pop(k, None)
        elif act == "stale":
            state[k] = {"state": "stale", "since": round(now, 1)}
        elif act == "active" and k in state:
            state.pop(k, None)
    _save(STATE, state)
    return {"archived": len(moved)}


def main() -> int:
    ap = argparse.ArgumentParser(description="经验生命周期：只归档不删除")
    ap.add_argument("--apply", action="store_true", help="执行（默认只预览）")
    ap.add_argument("--stale-days", type=float, default=STALE_DAYS)
    ap.add_argument("--archive-days", type=float, default=ARCHIVE_DAYS)
    ap.add_argument("--show", type=int, default=8, help="预览时列几条样例")
    args = ap.parse_args()
    rows, counts = plan(stale_days=args.stale_days, archive_days=args.archive_days)
    total = sum(counts.values())
    print(f"【经验生命周期】库 {total} 条 · stale≥{args.stale_days:.0f}天 · archive≥{args.archive_days:.0f}天")
    print(f"  active {counts['active']} / stale {counts['stale']} / archived {counts['archived']}"
          f" / 无时间戳(seed) {counts['seeded']}")
    for act in ("archived", "stale"):
        pick = [r for r in rows if r[2] == act][:args.show]
        for _k, text, _act, anchor in pick:
            when = time.strftime("%Y-%m-%d", time.localtime(anchor)) if anchor else "无时间戳"
            print(f"  [{act}] {when} {text[:60]}")
    if not args.apply:
        print("  预览模式，未改动任何文件；执行加 --apply")
        return 0
    res = apply(rows)
    print(f"  已执行：归档 {res['archived']} 条（进 {ARCHIVE.name}，可捞回），状态写入 {STATE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
