# -*- coding: utf-8 -*-
"""智能体档案技能 —— 配置子智能体：列出可用智能体、创建、改配置（技能/提示词）、删除。

派活之前先配人。一个子智能体 = 系统提示词（怎么想）+ 可用技能/工具（能做什么）+ 模型。
档案落盘 agent_profiles.json，派发时用 sub_agent_spawn(profile="coder") 指定。

工具：
- agent_profile_list   有哪些可用智能体（内置 5 个 + 自定义）
- agent_profile_show   某个档案的完整配置（含系统提示词全文 + 实际可用工具数）
- agent_profile_create 新建一个智能体
- agent_profile_update 改配置（系统提示词 / 可用技能 / 工具白名单 / 模型…）
- agent_profile_delete 删除自定义档案（内置不可删，需 confirm=true）
"""
from __future__ import annotations

import json
import re

# 可配置字段（与 agent_profiles.EDITABLE 保持一致）
_FIELDS = ("name", "description", "system_prompt", "skills", "tools",
           "deny_tools", "model", "max_rounds")
_LIST_FIELDS = ("skills", "tools", "deny_tools")


def _ap():
    import agent_profiles
    return agent_profiles


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _split_list(v) -> list[str]:
    """接受数组或逗号/空格分隔的字符串 → 字符串列表。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        items = re.split(r"[,，;；\s]+", str(v))
    return [x.strip() for x in items if x.strip()]


def _norm_skills(v) -> list[str]:
    """技能白名单：传 all / * / 全部 表示不限（空列表）。"""
    items = _split_list(v)
    if any(x.lower() in ("all", "*", "全部", "不限") for x in items):
        return []
    return items


def _patch_from_args(args: dict) -> dict:
    """从工具参数里抽出可配置字段（列表字段做拆分与规范化）。"""
    out: dict = {}
    for k in _FIELDS:
        if k not in args or args.get(k) is None:
            continue
        if k in _LIST_FIELDS:
            out[k] = _norm_skills(args.get(k)) if k == "skills" else _split_list(args.get(k))
        elif k == "max_rounds":
            try:
                out[k] = int(args.get(k))
            except Exception:
                continue
        else:
            out[k] = str(args.get(k))
    return out


def _tool_count(profile: dict) -> str:
    """这个档案实际能拿到多少工具（分母=全量工具池）。

    让「配置是否真的生效」可见，而不是靠感觉：改完技能白名单，
    这里的分母不变、分子会变，一眼就能看出限制真的生效了。
    """
    try:
        from agent import load_local_tools
        ap = _ap()
        pool = list(load_local_tools()) + ap.extra_skill_tool_specs([], all_when_empty=True)
        seen: set = set()
        merged: list = []
        for t in pool:
            n = str(((t or {}).get("function") or {}).get("name") or "")
            if n and n not in seen:
                seen.add(n)
                merged.append(t)
        kept = ap.resolve_tools(profile, merged)
        return f"{len(kept)}/{len(merged)} 个工具可用"
    except Exception as e:
        return f"（工具数估算失败：{e}）"


def agent_profile_list(args: dict) -> str:
    ap = _ap()
    profs = ap.list_profiles()
    builtin = [p for p in profs if p.get("builtin")]
    custom = [p for p in profs if not p.get("builtin")]
    lines = [f"可用智能体：{len(builtin)} 个内置 + {len(custom)} 个自定义"
             f"（档案文件 {ap.PROFILES_PATH}）"]
    for p in profs:
        tag = "内置" if p.get("builtin") else "自定义"
        lines.append(f"- `{p['id']}` {p['name']}（{tag}）｜{ap.describe(p)}")
        if p.get("description"):
            lines.append(f"    {p['description']}")
    lines.append("用法：派活时指定 sub_agent_spawn(task=\"…\", profile=\"coder\")；"
                 "看全文 agent_profile_show；改配置 agent_profile_update；"
                 "新建 agent_profile_create。")
    return "\n".join(lines)


def agent_profile_show(args: dict) -> str:
    ap = _ap()
    pid = str(args.get("profile_id") or args.get("profile") or "").strip()
    if not pid:
        return _err(f"需要 profile_id。现有：{', '.join(ap.profile_ids())}")
    p = ap.get_profile(pid)
    if not p:
        return _err(f"没有档案 {pid}。现有：{', '.join(ap.profile_ids())}")
    lines = [f"🧩 智能体档案 `{p['id']}` {p['name']}"
             f"（{'内置，可改不可删' if p.get('builtin') else '自定义'}）",
             f"说明：{p.get('description') or '（无）'}",
             f"可用技能：{', '.join(p.get('skills') or []) or '全部'}",
             f"工具白名单：{', '.join(p.get('tools') or []) or '（不设，随技能）'}",
             f"禁用工具：{', '.join(p.get('deny_tools') or []) or '（无）'}",
             f"模型覆盖：{p.get('model') or '（跟随主智能体）'}｜轮次上限：{p.get('max_rounds') or '不限'}",
             f"规模：{_tool_count(p)}",
             "系统提示词："]
    prompt = p.get("system_prompt") or ""
    lines.append(prompt if prompt else "（空——纯通用执行者，不附加角色约束）")
    return "\n".join(lines)


def agent_profile_create(args: dict) -> str:
    ap = _ap()
    pid = str(args.get("profile_id") or args.get("id") or "").strip()
    fields = _patch_from_args(args)
    if not pid and not fields.get("name"):
        return _err("至少给 profile_id 或 name（如 profile_id=\"reviewer\", "
                    "name=\"代码审查员\"）；建议英文 id，中文也能用。")
    try:
        p = ap.create_profile(pid, fields)
    except Exception as e:
        return _err(f"创建失败：{e}")
    return (f"✅ 已创建智能体 `{p['id']}` {p['name']}｜{ap.describe(p)}\n"
            f"系统提示词长度：{len(p.get('system_prompt') or '')} 字｜{_tool_count(p)}\n"
            f"派活：sub_agent_spawn(task=\"…\", profile=\"{p['id']}\")")


def agent_profile_update(args: dict) -> str:
    ap = _ap()
    pid = str(args.get("profile_id") or args.get("id") or "").strip()
    if not pid:
        return _err(f"需要 profile_id。现有：{', '.join(ap.profile_ids())}")
    fields = _patch_from_args(args)
    if not fields:
        return _err("没有可更新的字段。可用：" + "、".join(_FIELDS))
    try:
        p = ap.update_profile(pid, fields)
    except Exception as e:
        return _err(f"更新失败：{e}")
    return (f"✅ 已更新 `{p['id']}`（改了：{', '.join(fields)}）｜{ap.describe(p)}\n"
            f"系统提示词长度：{len(p.get('system_prompt') or '')} 字｜{_tool_count(p)}\n"
            f"提示：档案对「新派出」的子智能体立即生效，正在跑的不受影响。")


def agent_profile_delete(args: dict) -> str:
    ap = _ap()
    pid = str(args.get("profile_id") or args.get("id") or "").strip()
    if not pid:
        return _err(f"需要 profile_id。现有：{', '.join(ap.profile_ids())}")
    if not args.get("confirm"):
        p = ap.get_profile(pid)
        if not p:
            return _err(f"没有档案 {pid}。现有：{', '.join(ap.profile_ids())}")
        return _err(f"删除不可逆：即将删除自定义档案 `{pid}` {p['name']}"
                    f"（内置档案不可删）。确认请重发并带 confirm=true。")
    try:
        ap.delete_profile(pid)
    except Exception as e:
        return _err(f"删除失败：{e}")
    return (f"✅ 已删除智能体 `{pid}`。现有：{', '.join(ap.profile_ids())}"
            "（内置 5 个始终可用：general/coder/tester/researcher/writer）")


HANDLERS = {
    "agent_profile_list": agent_profile_list,
    "agent_profile_show": agent_profile_show,
    "agent_profile_create": agent_profile_create,
    "agent_profile_update": agent_profile_update,
    "agent_profile_delete": agent_profile_delete,
}
