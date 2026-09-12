"""场景脚本：把「看屏→解析→滚屏→再解析」压成一次调用。

存在的理由：单步原语（see/swipe/tap）已经够快，但一条真实任务要几十步，
每步过一次 LLM 往返就是几十秒。场景在进程内跑完，只把结构化结果交回来。

已实现：
  notes  滚屏采集信息流卡片（小红书/微博这类「类型 + 标题 + 来自作者 + N赞」列表）

命令行：
  venv/bin/python skills/android/scenario.py notes [条数]
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb_ui as ui

DATA = Path(__file__).resolve().parents[2] / "data" / "android"

CARD = re.compile(r"^(笔记|视频)\s+(.+)$")
NUM = re.compile(r"^[\d.]+万?$")


def _likes_num(v: str) -> int:
    if not v:
        return 0
    if v.endswith("万"):
        return int(float(v[:-1]) * 10000)
    return int(float(v))


def parse_card(text: str) -> dict | None:
    """把一条卡片文本拆成 {kind,title,author,likes}。拆不动返回 None。

    样本：'视频  为啥AI博主集体停更 来自海大厨 74赞'
          '笔记  INTP，冲着这几个方向努力就行了 来自银姑 赞'   ← 0 赞时没有数字
    """
    m = CARD.match((text or "").strip())
    if not m:
        return None
    kind, body = m.group(1), m.group(2)
    head, sep, tail = body.rpartition(" 来自")
    title = head.strip()
    if not sep or not title:
        return None
    tail = tail.strip()
    if tail.endswith("赞"):
        tail = tail[:-1].strip()
    parts = tail.rsplit(" ", 1)
    if len(parts) == 2 and NUM.match(parts[1]):
        author, likes = parts[0].strip(), parts[1]
    else:
        author, likes = tail, ""
    if not author:
        return None
    return {"kind": kind, "title": title, "author": author,
            "likes": likes, "likes_n": _likes_num(likes)}


def screen_size() -> tuple[int, int]:
    _, out = ui.sh("shell", "wm", "size")
    m = re.search(r"(\d+)x(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def swipe_up(w: int, h: int, frac: float = 0.55) -> None:
    x = w // 2
    y1 = int(h * 0.78)
    ui.sh("shell", "input", "swipe", str(x), str(y1), str(x), str(int(y1 - h * frac)), "400")


def wait_new_snap(prev_ts: float, timeout: float = 12.0) -> dict | None:
    """等实时层 dump 出新一屏。滚完立刻读缓存会拿到滚动前的旧屏，且它长得完全正常。"""
    end = time.time() + timeout
    while time.time() < end:
        s = ui.fresh_snap(ui.FRESH["any"])
        if s and s.get("ts", 0) > prev_ts + 0.3:
            return s
        time.sleep(0.4)
    return ui.fresh_snap(ui.FRESH["any"])


def collect_notes(n: int = 12, max_swipes: int = 20, app: str = "") -> str:
    s = ui.fresh_snap(ui.FRESH["look"])
    win = (s or {}).get("win") or ""
    if app and app not in win:
        return f"✗ 当前不在 {app}（现在：{win or '无快照'}）"
    w, h = screen_size()
    found: dict[str, dict] = {}
    swipes = 0
    while len(found) < n and swipes <= max_swipes:
        s = ui.fresh_snap(ui.FRESH["look"]) or wait_new_snap(0, 8)
        if not s:
            break
        for it in s.get("texts") or []:
            rec = parse_card(it.get("t") or "")
            if rec and rec["title"] not in found:
                rec["pos"] = it.get("c")
                found[rec["title"]] = rec
        if len(found) >= n:
            break
        prev = s.get("ts", 0)
        swipe_up(w, h)
        swipes += 1
        wait_new_snap(prev)
        time.sleep(0.6)
    items = list(found.values())[:n]
    if not items:
        return "✗ 一条卡片都没解析出来（页面结构变了？先用 see 看一眼）"
    stamp = int(time.time())
    out = DATA / f"collect_{stamp}.json"
    DATA.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ts": stamp, "win": win, "swipes": swipes,
                               "count": len(items), "items": items},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    body = "\n".join(
        f"{i}. [{r['kind']}] {r['title'][:44]} —— {r['author']} · {r['likes_n']}赞"
        for i, r in enumerate(items, 1))
    return (f"采集 {len(items)} 条（滚屏 {swipes} 次，共 {len(found)} 条去重后）"
            f"\n落盘 {out}\n{body}")


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    if a[0] == "notes":
        print(collect_notes(int(a[1]) if len(a) > 1 else 12))
        return 0
    print(f"✗ 未知场景 {a[0]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
