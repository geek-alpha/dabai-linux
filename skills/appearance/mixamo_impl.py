# -*- coding: utf-8 -*-
"""Mixamo 动作库技能 —— 大白自主下载 / 管理 / 优化自己的表情动作。

- 下载：复用 mixamo_download_service（Playwright + fq 代理，自动选 Without Skin），
  服务与 server.py 共用同一单例，状态永远一致；
- 管理：直接操作 web/anim/ 目录与 animation-library.json 配置（扫描/归类/校验/清单）；
- 优化：校验配置结构、情绪映射一致性，并自动修复。

登录是唯一需要用户参与的环节（Adobe 会话会过期）：启动浏览器后若未登录，
需让用户在弹出窗口登录一次，再用 anim_check_login 确认、anim_save_cookies 保存，
之后批量下载即可全自动。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ANIM_DIR = BASE_DIR / "web" / "anim"
CONFIG_PATH = ANIM_DIR / "animation-library.json"

MAX_OUT = 5000


def _clip(text: str, limit: int = MAX_OUT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:3200] + "\n…（中间省略）…\n" + text[-(limit - 3400):]


# ---------- 动作库配置读写 ----------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"categories": {}, "emotionMap": {}}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"categories": {}, "emotionMap": {}}


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _all_anims(cfg: dict) -> list[dict]:
    out = []
    for cat_key, cat in cfg.get("categories", {}).items():
        for anim in cat.get("animations", []):
            out.append({**anim, "category": cat_key,
                        "category_label": cat.get("label", cat_key)})
    return out


def _normalize(name: str) -> str:
    return re.sub(r"[\s_\-\.]", "", (name or "").lower())


def _match_anim(filename: str, anims: list[dict]) -> dict | None:
    """把磁盘文件名匹配到配置条目（直接匹配优先，否则按字符重叠相似度）。"""
    base = re.sub(r"\.fbx$", "", filename, flags=re.I)
    nb = _normalize(base)
    best, best_score = None, 0.0
    for anim in anims:
        cfg_base = re.sub(r"\.fbx$", "", anim["file"].split("/")[-1], flags=re.I)
        nc = _normalize(cfg_base)
        if nc == nb:
            return anim
        common = sum(1 for c in nc if c in nb)
        score = common / max(len(nc), 1)
        if score > best_score:
            best_score, best = score, anim
    return best if best_score >= 0.5 else None


def _scan_disk() -> list[str]:
    """扫描 web/anim/ 下所有 .fbx 相对路径（含分类子目录）。"""
    if not ANIM_DIR.is_dir():
        return []
    return sorted(p.relative_to(ANIM_DIR).as_posix() for p in ANIM_DIR.rglob("*.fbx"))


def _verify(cfg: dict) -> tuple[list[dict], list[dict], list[str]]:
    anims = _all_anims(cfg)
    missing = [a for a in anims if not (ANIM_DIR / a["file"]).exists()]
    disk = set(_scan_disk())
    expected = {a["file"] for a in anims}
    orphans = sorted(disk - expected)
    return anims, missing, orphans


# ---------- 下载服务（懒加载，避免 playwright 缺失导致技能加载失败） ----------

async def _svc():
    from mixamo_download_service import service
    return service


async def anim_status(args: dict) -> str:
    svc = await _svc()
    st = svc.get_status()
    cfg = _load_config()
    anims, missing, orphans = _verify(cfg)

    lines = ["【下载服务】"]
    lines.append(f"浏览器: {'运行中' if st['is_running'] else '未启动'} | "
                 f"登录: {'已登录' if st['is_logged_in'] else '未登录'} | "
                 f"代理: {st.get('proxy') or '-'} ({st.get('proto') or '-'})")
    if st.get("current_download"):
        lines.append(f"正在下载: {st['current_download']} | 队列剩余: {st.get('queue_remaining', 0)}")
    s = st.get("stats") or {}
    lines.append(f"本次统计: 成功{s.get('success', 0)} 失败{s.get('failed', 0)} 跳过{s.get('skipped', 0)}")

    lines.append("")
    lines.append("【动作库】")
    by_cat: dict[str, dict] = {}
    for a in anims:
        b = by_cat.setdefault(a["category"], {"label": a["category_label"], "total": 0, "have": 0})
        b["total"] += 1
        if (ANIM_DIR / a["file"]).exists():
            b["have"] += 1
    for k, b in by_cat.items():
        lines.append(f"  {k}({b['label']}): {b['have']}/{b['total']}")
    lines.append(f"合计: {len(anims) - len(missing)}/{len(anims)} 个动作在盘")
    if missing:
        lines.append(f"缺失 {len(missing)} 个: " + ", ".join(a["name"] for a in missing[:15])
                     + ("…" if len(missing) > 15 else ""))
    if orphans:
        lines.append(f"未注册文件 {len(orphans)} 个: " + ", ".join(orphans[:10])
                     + ("…" if len(orphans) > 10 else ""))

    if st.get("logs"):
        lines.append("")
        lines.append("【最近日志】")
        for e in st["logs"][-8:]:
            lines.append(f"  [{e['time']}] {e['msg']}")
    return _clip("\n".join(lines))


async def anim_start(args: dict) -> str:
    svc = await _svc()
    try:
        r = await svc.start(proto=args.get("proto"))
    except Exception as e:
        return f"启动失败: {e}"
    if r.get("status") == "already_running":
        return "浏览器已在运行。\n" + await anim_status(args)
    return (f"浏览器已启动（代理 {r.get('proxy')}）。登录状态: "
            f"{'已登录' if r.get('is_logged_in') else '未登录'}。"
            "若未登录，请让用户在弹出窗口登录 Mixamo/Adobe，然后 anim_check_login 确认。")


async def anim_stop(args: dict) -> str:
    svc = await _svc()
    try:
        r = await svc.stop()
        return f"浏览器已关闭（{r.get('status')}）。"
    except Exception as e:
        return f"关闭失败: {e}"


async def anim_check_login(args: dict) -> str:
    svc = await _svc()
    if not svc.is_running:
        return "浏览器未启动，请先 anim_start。"
    try:
        logged = await svc.check_login_cookies()
    except Exception as e:
        return f"检测登录失败: {e}"
    if logged:
        return "已登录 Mixamo。可以 anim_save_cookies 保存凭据，或直接 anim_download / anim_batch 下载。"
    return ("未登录。请让用户在弹出浏览器窗口完成 Mixamo/Adobe 登录，"
            "登录完成后再次 anim_check_login 确认，然后 anim_save_cookies 保存凭据。")


async def anim_save_cookies(args: dict) -> str:
    svc = await _svc()
    if not svc.is_running:
        return "浏览器未启动，请先 anim_start。"
    try:
        r = await svc.save_cookies()
        return f"已保存 {r.get('count', 0)} 条 cookies，登录状态标记为已登录。"
    except Exception as e:
        return f"保存 cookies 失败: {e}"


async def anim_download(args: dict) -> str:
    svc = await _svc()
    if not svc.is_running:
        return "浏览器未启动，请先 anim_start。"
    name = str(args.get("name") or "").strip()
    if not name:
        return "需要 name 参数（动作名，如 idle_normal）。"
    try:
        r = await svc.download_animation(name)
    except Exception as e:
        return f"下载失败: {e}"
    if r.get("status") == "success":
        return f"✓ {name} 已下载到 {r.get('path')}（Without Skin 纯动作）。"
    if r.get("status") == "skipped":
        return f"{name} 已存在，跳过（{r.get('path')}）。"
    return f"✗ {name} 下载失败: {r.get('error')}"


async def anim_batch(args: dict) -> str:
    svc = await _svc()
    if not svc.is_running:
        return "浏览器未启动，请先 anim_start。"
    names = args.get("names") or []
    if not names:
        return "需要 names 参数（动作名数组，如 [\"idle_normal\",\"wave\"]）。"
    if not svc.is_logged_in:
        try:
            logged = await svc.check_login_cookies()
        except Exception:
            logged = False
        if not logged:
            return ("未登录 Mixamo，无法批量下载。请先让用户在弹出窗口登录，"
                    "再 anim_check_login 确认 + anim_save_cookies 保存，然后重试。")
    # 与 server.py 一致：取消旧任务 + 清 stop 标记 + 持引用防 GC
    if getattr(svc, "_task", None) and not svc._task.done():
        svc._task.cancel()
    svc._stop_event.clear()
    svc._task = asyncio.create_task(svc.batch_download(list(names)))
    return (f"批量下载已启动：{len(names)} 个动作（后台执行）。"
            f"用 anim_status 轮询进度；anim_stop 可中止。")


# ---------- 动作库管理 ----------

async def anim_library(args: dict) -> str:
    action = str(args.get("action") or "stats").strip().lower()
    cfg = _load_config()
    anims, missing, orphans = _verify(cfg)

    if action == "stats":
        return await anim_status(args)

    if action == "list":
        cat = str(args.get("category") or "").strip()
        emo = str(args.get("emotion") or "").strip()
        status = str(args.get("status") or "").strip()
        rows = []
        for a in anims:
            if cat and a["category"] != cat:
                continue
            if emo and a.get("emotion") != emo:
                continue
            have = (ANIM_DIR / a["file"]).exists()
            if status == "missing" and have:
                continue
            if status == "have" and not have:
                continue
            rows.append(f"  {'✓' if have else '✗'} {a['name']:<22} [{a['category']}] "
                        f"emotion={a.get('emotion', '-')}  {a['file']}")
        head = f"共 {len(rows)} 条"
        if cat or emo or status:
            head += f"（筛选: {cat or '-'}/{emo or '-'}/{status or 'all'}）"
        return head + "\n" + "\n".join(rows) if rows else head + "（无匹配）"

    if action == "scan":
        disk = _scan_disk()
        if not disk:
            return "web/anim/ 下没有 .fbx 文件。"
        lines = [f"磁盘共 {len(disk)} 个 .fbx 文件："]
        for f in disk:
            match = _match_anim(Path(f).name, anims)
            tag = f"→ {match['file']}" if match else "（未匹配配置）"
            lines.append(f"  {f}  {tag}")
        return "\n".join(lines)

    if action == "verify":
        lines = [f"配置 {len(anims)} 个动作，磁盘 {len(_scan_disk())} 个文件。"]
        lines.append(f"缺失 {len(missing)} 个：")
        for a in missing:
            lines.append(f"  ✗ {a['name']}  {a['file']}")
        lines.append(f"未注册 {len(orphans)} 个：")
        for f in orphans:
            lines.append(f"  ? {f}")
        return "\n".join(lines)

    if action == "categorize":
        dry = bool(args.get("dry_run", True))
        moved = []
        expected = {a["file"] for a in anims}
        for f in _scan_disk():
            if f in expected:
                continue
            match = _match_anim(Path(f).name, anims)
            if match and match["file"] != f:
                src = ANIM_DIR / f
                dst = ANIM_DIR / match["file"]
                if dry:
                    moved.append(f"将移动 {f} → {match['file']}")
                else:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dst))
                        moved.append(f"已移动 {f} → {match['file']}")
                    except Exception as e:
                        moved.append(f"移动失败 {f}: {e}")
        if not moved:
            return "没有需要归类的文件（都在正确位置）。"
        head = "（dry_run 预览，未实际移动）" if dry else "已归类："
        return head + "\n" + "\n".join(moved)

    return ("未知 action。可用：stats / list / scan / verify / categorize。"
            "详见 skill_help(\"mixamo-anim\")。")


# ---------- 优化 ----------

async def anim_optimize(args: dict) -> str:
    action = str(args.get("action") or "validate").strip().lower()
    cfg = _load_config()
    anims = _all_anims(cfg)
    emap = cfg.get("emotionMap", {})

    if action == "emotions":
        if not emap:
            return "配置中没有 emotionMap。"
        lines = ["情绪映射（emotionMap）："]
        for emo in sorted(emap):
            clips = emap[emo]
            have = sum(1 for c in clips if (ANIM_DIR / next(
                (a["file"] for a in anims if a["name"] == c), "")).exists())
            lines.append(f"  {emo:<12} {len(clips)} 个: {', '.join(clips)}  (在盘 {have}/{len(clips)})")
        return "\n".join(lines)

    if action == "validate":
        issues = []
        names = [a["name"] for a in anims]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            issues.append(f"重复动作名: {dupes}")
        for a in anims:
            if not a.get("file"):
                issues.append(f"{a.get('name')} 缺少 file")
            if not a.get("emotion"):
                issues.append(f"{a.get('name')} 缺少 emotion")
        valid = set(names)
        for emo, clips in emap.items():
            for c in clips:
                if c not in valid:
                    issues.append(f"emotionMap[{emo}] 引用了不存在的动作: {c}")
        referenced = {c for clips in emap.values() for c in clips}
        for a in anims:
            if a["name"] not in referenced:
                issues.append(f"动作 {a['name']} 未出现在任何 emotionMap 中")
        if not issues:
            return "配置校验通过：无重复名、无缺失字段、情绪映射引用全部有效。"
        return f"发现 {len(issues)} 个问题：\n" + "\n".join(f"  - {i}" for i in issues)

    if action == "fix":
        if not emap:
            emap = cfg.setdefault("emotionMap", {})
        fixed = []
        valid = {a["name"] for a in anims}
        for emo in list(emap):
            before = len(emap[emo])
            emap[emo] = [c for c in emap[emo] if c in valid]
            if len(emap[emo]) != before:
                fixed.append(f"清理 emotionMap[{emo}] 无效引用")
        referenced = {c for clips in emap.values() for c in clips}
        for a in anims:
            if a["name"] not in referenced:
                emo = a.get("emotion") or "neutral"
                emap.setdefault(emo, []).append(a["name"])
                fixed.append(f"把 {a['name']} 加入 emotionMap[{emo}]")
        if not fixed:
            return "无需修复，配置已一致。"
        _save_config(cfg)
        return "已修复：\n" + "\n".join(f"  - {i}" for i in fixed)

    return ("未知 action。可用：validate / emotions / fix。"
            "详见 skill_help(\"mixamo-anim\")。")


HANDLERS = {
    "anim_status": anim_status,
    "anim_start": anim_start,
    "anim_stop": anim_stop,
    "anim_check_login": anim_check_login,
    "anim_save_cookies": anim_save_cookies,
    "anim_download": anim_download,
    "anim_batch": anim_batch,
    "anim_library": anim_library,
    "anim_optimize": anim_optimize,
}
