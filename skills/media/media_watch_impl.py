# -*- coding: utf-8 -*-
"""媒体子智能体看护技能 —— 主智能体查看/管理自己派出去的子进程。

与 skills/media 的 music/video 的 watch=true 配合：
- music_play / music_play_playlist / video_play 带 watch=true 时返回
  __media_watch__ JSON → server 派出媒体子智能体（media_workers 注册表 +
  任务中心条目），播完自动汇报给主智能体；
- 这里的三个工具是「看护台」：列出在干活的子进程、看单个详情、收回某个子进程。
"""
from __future__ import annotations

import json


def _mw():
    from media_workers import get_media_workers
    return get_media_workers()


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def media_workers_list(args: dict) -> str:
    try:
        items = _mw().list(50)
    except Exception as e:
        return _err(f"子智能体注册表不可用: {e}")
    active = [w for w in items if w["status"] in ("playing", "paused")]
    if not active:
        return "当前没有正在干活的媒体子智能体（没有派出 watch 的播放，或都已播完/结束）。"
    lines = [f"当前有 {len(active)} 个媒体子智能体在干活："]
    for w in active:
        lines.append(f"- {w['icon']} {w['kind_label']}《{w['title']}》[{w['id']}] {w['status_label']}")
    done = [w for w in items if w["status"] in ("done", "error", "cancelled")]
    if done:
        lines.append("（最近结束 " + str(len(done)) + " 个："
                     + "、".join(d["title"][:20] for d in done[:3]) + "…）")
    return "\n".join(lines)


def media_worker_status(args: dict) -> str:
    wid = str(args.get("worker_id") or "").strip()
    if not wid:
        return _err("需要 worker_id（media_workers_list 可查）。")
    try:
        w = _mw().get(wid)
    except Exception as e:
        return _err(f"查询失败: {e}")
    if w is None:
        return _err(f"子智能体 {wid} 不存在或已被清理。")
    s = w.snapshot(full=True)
    lines = [
        f"{s['icon']} 子智能体 [{s['id']}]：{s['kind_label']}《{s['title']}》—— {s['status_label']}",
        f"任务：{s['brief'] or '（无描述）'}",
    ]
    if s.get("result"):
        lines.append(f"结果：{s['result'][:300]}")
    if s.get("error"):
        lines.append(f"错误：{s['error'][:300]}")
    if s.get("task_id"):
        lines.append(f"任务中心条目：{s['task_id']}")
    return "\n".join(lines)


def media_worker_cancel(args: dict) -> str:
    wid = str(args.get("worker_id") or "").strip()
    if not wid:
        return _err("需要 worker_id（media_workers_list 可查）。")
    reason = str(args.get("reason") or "").strip() or "用户要求收回"
    try:
        w = _mw().get(wid)
    except Exception as e:
        return _err(f"查询失败: {e}")
    if w is None:
        return _err(f"子智能体 {wid} 不存在或已结束。")
    # 返回取消标记 → server 取消 worker + 向前端发停止播放的屏幕指令
    stop_tool = "control_video" if w.kind == "video" else "stop_music"
    if stop_tool == "control_video":
        stop_args = {"action": "stop", "message": f"已收回子智能体并停止播放《{w.title}》。"}
    else:
        stop_args = {"message": f"已收回子智能体并停止播放《{w.title}》。"}
    return json.dumps({
        "__media_watch_cancel__": True,
        "worker_id": wid,
        "reason": reason,
        "screen": {"tool": stop_tool, "args": stop_args},
    }, ensure_ascii=False)


HANDLERS = {
    "media_workers_list": media_workers_list,
    "media_worker_status": media_worker_status,
    "media_worker_cancel": media_worker_cancel,
}
