#!/usr/bin/env python3
"""工具结果落盘索引的检索入口 —— 让被压缩掉的工具结果能被「找回来」。

背景：工具结果超长时全文落在 data/tool_spill/，按内容哈希命名。哈希名本身不含任何
语义，一旦上下文里的路径指针滚走，文件就等于不存在。index.jsonl 给每条落盘记录补上
时间/工具名/字数/预览，这个 CLI 就是翻它的手：

    python tools/spill_index.py list --n 20          # 最近落盘的 20 条
    python tools/spill_index.py list --tool shell_run
    python tools/spill_index.py search 端口占用       # 预览 + 全文一起搜
    python tools/spill_index.py show 7bd5a007c8      # 打印正文（前缀匹配）
"""
import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import memory  # noqa: E402

SPILL_DIR = memory._TOOL_SPILL_DIR
INDEX = memory._TOOL_SPILL_INDEX


def _load() -> list:
    if not INDEX.exists():
        return []
    out = []
    for line in INDEX.read_text(encoding='utf-8').splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict) or not e.get('file'):
            continue
        out.append(e)
    return out


def _fmt(e: dict, width: int = 70) -> str:
    ts = time.strftime('%m-%d %H:%M', time.localtime(e.get('ts', 0)))
    gone = '' if (SPILL_DIR / e['file']).exists() else ' [文件已不在]'
    tool = (e.get('tool') or '-')[:14]
    preview = (e.get('preview') or '')[:width]
    return f"{ts}  {e['file']:<16} {e.get('chars', 0):>7}字  {tool:<14} {preview}{gone}"


def cmd_list(args) -> int:
    items = _load()
    if args.tool:
        items = [e for e in items if args.tool.lower() in (e.get('tool') or '').lower()]
    items.sort(key=lambda e: e.get('ts', 0), reverse=True)
    items = items[:args.n]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("索引里没有匹配记录（工具结果超长时才会落盘）")
        return 0
    for e in items:
        print(_fmt(e))
    print(f"—— 共 {len(items)} 条；正文目录 {SPILL_DIR}")
    return 0


def cmd_search(args) -> int:
    kw = args.keyword
    hits = []
    for e in _load():
        path = SPILL_DIR / e['file']
        where = []
        if kw in (e.get('preview') or ''):
            where.append('预览')
        if path.exists():
            try:
                text = path.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                text = ''
            if kw in text:
                where.append('全文')
        if where:
            hits.append((e, where, text if path.exists() else ''))
    hits.sort(key=lambda x: x[0].get('ts', 0), reverse=True)
    if not hits:
        print(f"没有落盘结果包含「{kw}」")
        return 0
    for e, where, text in hits[:args.n]:
        print(f"[{'/'.join(where)}] {_fmt(e)}")
        if '全文' in where and not args.json:
            for i, line in enumerate(text.splitlines(), 1):
                if kw in line:
                    print(f"    {i}: {line.strip()[:160]}")
    print(f"—— 命中 {len(hits)} 条")
    return 0


def cmd_show(args) -> int:
    key = args.name
    cands = [e for e in _load() if e['file'].startswith(key) or key in (e.get('preview') or '')]
    if not cands:
        print(f"索引里没有匹配「{key}」的记录")
        return 1
    e = sorted(cands, key=lambda x: x.get('ts', 0), reverse=True)[0]
    path = SPILL_DIR / e['file']
    if not path.exists():
        print(f"{e['file']} 已被剪枝删除（索引里还留着，落盘于 {time.strftime('%m-%d %H:%M', time.localtime(e.get('ts', 0)))}）")
        return 1
    print(f"# {path}  ({e.get('chars', 0)}字, {e.get('tool') or '未知工具'})")
    print(path.read_text(encoding='utf-8', errors='ignore'))
    return 0


def cmd_backfill(args) -> int:
    """给索引出现前就已落盘的旧文件补条目（ts 取文件 mtime，保持时间序）。

    没有这步，39 个旧文件对 CLI 就是隐形的——它们在上下文里没指针时就真的丢了。
    """
    known = {e['file'] for e in _load()}
    added = 0
    for p in sorted(SPILL_DIR.glob('*.txt')):
        if p.name in known:
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
            mtime = p.stat().st_mtime
        except OSError:
            continue
        memory._spill_index_append({
            'ts': mtime, 'file': p.name, 'chars': len(text), 'tool': '',
            'preview': " ".join(text[:memory._TOOL_SPILL_PREVIEW_CHARS].split()),
            'backfill': True})
        added += 1
    dead = {e['file'] for e in _load() if not (SPILL_DIR / e['file']).exists()}
    if dead:
        memory._spill_index_drop(dead)
    print(f"回填 {added} 条，清掉 {len(dead)} 条死指针（索引现有 {len(_load())} 条）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="工具结果落盘索引检索")
    sub = p.add_subparsers(dest='cmd')

    pl = sub.add_parser('list', help='列出最近的落盘记录')
    pl.add_argument('--n', type=int, default=20)
    pl.add_argument('--tool', default='', help='按工具名过滤')
    pl.add_argument('--json', action='store_true')
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser('search', help='按关键词搜预览与全文')
    ps.add_argument('keyword')
    ps.add_argument('--n', type=int, default=10)
    ps.add_argument('--json', action='store_true')
    ps.set_defaults(func=cmd_search)

    ph = sub.add_parser('show', help='打印某条落盘正文（文件名前缀或预览关键词）')
    ph.add_argument('name')
    ph.set_defaults(func=cmd_show)

    pb = sub.add_parser('backfill', help='给索引出现前落盘的旧文件补条目')
    pb.set_defaults(func=cmd_backfill)

    args = p.parse_args()
    if not getattr(args, 'func', None):
        p.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
