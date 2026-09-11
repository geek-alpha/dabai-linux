# -*- coding: utf-8 -*-
"""智能体档案（Agent Profiles）—— 配置子智能体的「人格 + 能力 + 预算」。

第一性原理：一个子智能体 = 系统提示词（怎么想）× 工具集（能做什么）× 模型与预算（花多少）。
默认派出的子智能体是「全能力通用执行者」；但大型任务里不同模块需要不同角色——
测试员不该顺手改实现、文档写手不该重构代码、调查员不该写文件。
档案把这三件事收敛成一份可复用配置，落盘 agent_profiles.json，可查、可改、可派。

三层工具过滤（顺序敏感，前一层优先）：
1. deny_tools  黑名单（支持 fnmatch 通配，如 sub_agent_*）；
2. tools       白名单：非空则只留这些工具名（最精确）；
3. skills      技能白名单：只留这些技能的工具，外加「基础工具」。

为什么基础工具（读写文件/命令/搜索）永远保留：白名单的目的是防越界，不是把子智能体弄残。
一个连文件都读不了的 worker 只会烧完 token 交一份失败汇报——那比不派还贵。

诚实边界：shell_run 与文件工具本身就能做成任何事，所以档案是「引导与聚焦」，不是安全沙箱。
真正的隔离靠任务边界与并发槽位，不靠提示词。
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROFILES_PATH = BASE_DIR / "agent_profiles.json"
SKILLS_DIR = BASE_DIR / "skills"

MAX_PROMPT = 4000          # 单档案系统提示词上限（防把上下文塞爆）
MAX_ID = 48

_LOCK = threading.RLock()
_FILE_CACHE = {"key": "", "data": None}      # 按 mtime+size 缓存，热改档案即时生效
_SKILL_MAP_CACHE = {"key": "", "map": {}}

# 允许写入的字段（白名单，防止乱七八糟的键污染档案）
EDITABLE = ("name", "description", "system_prompt", "skills", "tools",
            "deny_tools", "model", "max_rounds")

# 「基本生存能力」技能：任何档案都隐式保留。
# 为什么：文件读写/命令/检索是子智能体完成任务的最低要求——白名单的目的是防越界
# （别改不该改的），不是把人弄残（连文件都读不了 = 必然交一份失败汇报，比不派还贵）。
# 想真正禁止写文件，用 deny_tools 点名禁用（如 code_edit,code_create_file）。
ALWAYS_SKILLS = ("code_ops",)


# ── 内置档案 ──────────────────────────────────────────────
# 内置档案可以改（改提示词/技能），但不允许删——删掉一个「测试员」只会让下次
# 有人想派测试任务时找不到人，不如让他改。自定义档案可自由增删。
BUILTIN: list[dict] = [
    {
        "id": "general",
        "name": "通用执行者",
        "description": "全能力默认子智能体：什么任务都能接，不确定用谁就用它。",
        "system_prompt": "",
        "skills": [],
        "tools": [],
        "deny_tools": [],
        "model": "",
        "max_rounds": 0,
    },
    {
        "id": "coder",
        "name": "核心开发",
        "description": "写实现代码：最小改动、改完必跑验证、只报证据不写客套话。",
        "system_prompt": (
            "你的角色：核心开发工程师。\n"
            "1. 只改任务点名范围内的文件，看到顺手能优化的无关代码也不要动——那不是你的范围；\n"
            "2. 先写出对外签名/契约，再填实现；改完必须跑一次最小验证"
            "（语法检查 / 单测 / 冒烟命令），把命令原文与关键输出原样报出；\n"
            "3. 结论只写四行：改了哪些文件 → 验收命令 → 关键输出 → 未完成或存疑项。不要客套话。"
        ),
        "skills": ["code_ops"],
        "tools": [],
        "deny_tools": [],
        "model": "",
        "max_rounds": 0,
    },
    {
        "id": "tester",
        "name": "测试工程师",
        "description": "只写测试、只报事实：给复现步骤与断言实际值，不顺手改实现。",
        "system_prompt": (
            "你的角色：测试工程师。\n"
            "1. 只新增/修改测试文件与测试数据，绝不去改被测实现——发现实现有 bug 就写一个"
            "能复现它的失败用例，把复现步骤与断言实际值写清楚，让实现方去改；\n"
            "2. 用例必须能独立跑：给定输入 → 断言输出，不依赖执行顺序、不依赖网络；\n"
            "3. 结论写：测试文件路径 → 运行命令 → 通过/失败数 → 失败用例的复现步骤。"
        ),
        "skills": ["code_ops"],
        "tools": [],
        "deny_tools": [],
        "model": "",
        "max_rounds": 0,
    },
    {
        "id": "researcher",
        "name": "调查员",
        "description": "只读调查：查资料、读代码、给路径与行号证据，不改任何文件。",
        "system_prompt": (
            "你的角色：调查员。\n"
            "1. 只读不写：不改任何文件、不提交、不安装依赖；需要动手的结论交给主智能体；\n"
            "2. 每个结论都必须带证据：`文件:行号` 或 URL，禁止凭印象下判断；\n"
            "3. 结论写：直接答案 → 证据清单（路径:行号 / 链接）→ 不确定的部分明确标注「未验证」。"
        ),
        "skills": ["search", "code_ops"],
        "tools": [],
        "deny_tools": [],
        "model": "",
        "max_rounds": 0,
    },
    {
        "id": "writer",
        "name": "文档写手",
        "description": "只写文档：结构清晰、示例可复制即用，不碰实现代码。",
        "system_prompt": (
            "你的角色：文档写手。\n"
            "1. 只写文档文件（README/docs/*.md），不改任何实现代码；\n"
            "2. 文档里的命令与代码示例必须来自真实文件（读过再写），能复制即用，不许编造参数；\n"
            "3. 结构优先：先给「怎么用」的最短路径，再给细节与边界说明；诚实写出已知限制。"
        ),
        "skills": ["code_ops"],
        "tools": [],
        "deny_tools": [],
        "model": "",
        "max_rounds": 0,
    },
]

# 模块类型 → 推荐档案（project_dispatch 按此自动选人）
KIND_PROFILE = {
    "data": "coder",
    "core": "coder",
    "api": "coder",
    "ui": "coder",
    "test": "tester",
    "doc": "writer",
    "research": "researcher",
}


# ── 底层读写 ──────────────────────────────────────────────
def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_profile_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def slug_id(text: str, fallback: str = "agent") -> str:
    """档案 id 规范化：只留字母/数字/下划线/连字符/CJK（\\w 默认 Unicode），其余转连字符。

    保留中文是为了容错（`测试员` 能直接当 id 用），但仍建议用英文 id：
    id 会出现在任务中心、日志与工具参数里，英文更不容易被误输入。
    """
    s = re.sub(r"[^\w\-]+", "-", str(text or "").strip().lower(), flags=re.UNICODE)
    return s.strip("-_")[:MAX_ID] or fallback


def _read_custom() -> list[dict]:
    """读自定义档案（带 mtime 缓存）。文件不存在/损坏 → 空列表，绝不让档案成为故障源。"""
    try:
        st = PROFILES_PATH.stat()
        key = f"{st.st_mtime_ns}:{st.st_size}"
    except Exception:
        return []
    with _LOCK:
        if _FILE_CACHE["key"] == key and _FILE_CACHE["data"] is not None:
            return _FILE_CACHE["data"]
    items: list[dict] = []
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("profiles") if isinstance(data, dict) else data
        if isinstance(raw, list):
            for it in raw:
                if isinstance(it, dict) and it.get("id"):
                    items.append(dict(it))
    except Exception:
        items = []
    with _LOCK:
        _FILE_CACHE["key"] = key
        _FILE_CACHE["data"] = items
    return items


def _write_custom(items: list[dict]) -> None:
    payload = {"version": 1, "updated_at": int(time.time()),
               "profiles": items}
    _atomic_write(PROFILES_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    with _LOCK:
        _FILE_CACHE["key"] = ""          # 让下次读取重新落盘
        _FILE_CACHE["data"] = None


# ── 档案 CRUD ─────────────────────────────────────────────
def _as_list(v) -> list[str]:
    """列表字段容错：数组 / 逗号或空格分隔的字符串都接受。

    必须容错字符串：不经工具层（agent_profiles_impl）直接调 API 时传 "code_ops"
    是常见写法；若按字符迭代就变成 ['c','o','d','e',...]——技能白名单静默失效，
    而且失效方式极隐蔽：不报错，只是工具全没了。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        items = [str(x) for x in v]
    else:
        items = re.split(r"[,，;；\s]+", str(v))
    return [x.strip() for x in items if x.strip()]


def _normalize(pid: str, src: dict, builtin: bool = False) -> dict:
    out = {
        "id": pid,
        "name": str(src.get("name") or pid)[:60],
        "description": str(src.get("description") or "")[:200],
        "system_prompt": str(src.get("system_prompt") or "")[:MAX_PROMPT],
        "skills": _as_list(src.get("skills")),
        "tools": _as_list(src.get("tools")),
        "deny_tools": _as_list(src.get("deny_tools")),
        "model": str(src.get("model") or "").strip(),
        "max_rounds": int(src.get("max_rounds") or 0),
        "builtin": bool(builtin),
    }
    return out


def list_profiles() -> list[dict]:
    """内置 + 自定义（同名自定义覆盖内置，但保留 builtin 标记 = 不可删）。"""
    out: dict[str, dict] = {}
    for b in BUILTIN:
        out[b["id"]] = _normalize(b["id"], b, builtin=True)
    for c in _read_custom():
        pid = slug_id(c.get("id"))
        if not pid:
            continue
        base = out.get(pid) or {}
        merged = dict(base)
        merged.update({k: v for k, v in c.items() if k in EDITABLE and v not in (None, "")})
        out[pid] = _normalize(pid, merged, builtin=bool(base.get("builtin")))
    return [out[k] for k in sorted(out)]


def get_profile(pid: str) -> dict | None:
    pid = slug_id(pid)
    if not pid:
        return None
    for p in list_profiles():
        if p["id"] == pid:
            return p
    return None


def profile_ids() -> list[str]:
    return [p["id"] for p in list_profiles()]


def create_profile(pid: str, fields: dict) -> dict:
    pid = slug_id(pid or fields.get("name"))
    if not pid:
        raise ValueError("档案 id 不能为空（可用 name 自动生成）")
    if get_profile(pid):
        raise ValueError(f"档案 {pid} 已存在（用 agent_profile_update 改它）")
    item = _normalize(pid, fields)
    item.pop("builtin", None)
    items = _read_custom()
    items.append(item)
    _write_custom(items)
    return get_profile(pid) or item


def update_profile(pid: str, fields: dict) -> dict:
    pid = slug_id(pid)
    cur = get_profile(pid)
    if not cur:
        raise ValueError(f"没有档案 {pid}（现有：{', '.join(profile_ids())}）")
    patch = {k: v for k, v in fields.items() if k in EDITABLE and v is not None}
    if not patch:
        raise ValueError("没有可更新的字段（可用：name/description/system_prompt/"
                         "skills/tools/deny_tools/model/max_rounds）")
    items = [c for c in _read_custom() if slug_id(c.get("id")) != pid]
    merged = {k: cur.get(k) for k in EDITABLE}
    merged.update(patch)
    item = _normalize(pid, merged)
    item.pop("builtin", None)
    items.append(item)
    _write_custom(items)
    return get_profile(pid) or item


def delete_profile(pid: str) -> str:
    pid = slug_id(pid)
    cur = get_profile(pid)
    if not cur:
        raise ValueError(f"没有档案 {pid}")
    if cur.get("builtin"):
        raise ValueError(f"{pid} 是内置档案，不能删除（可以 update 改它的提示词与技能）")
    items = [c for c in _read_custom() if slug_id(c.get("id")) != pid]
    _write_custom(items)
    return pid


# ── 技能 → 工具映射（读 skills/<name>/skill.json，不依赖 harness 是否已加载）──
def skill_tool_map(refresh: bool = False) -> dict[str, list[str]]:
    """{技能名: [工具名...]}。直接读盘，技能热重载后自动跟上。"""
    try:
        key = str(SKILLS_DIR.stat().st_mtime_ns)
    except Exception:
        return {}
    with _LOCK:
        if not refresh and _SKILL_MAP_CACHE["key"] == key:
            return _SKILL_MAP_CACHE["map"]
    out: dict[str, list[str]] = {}
    try:
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            mf = d / "skill.json"
            if not mf.exists():
                continue
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            name = str(data.get("name") or d.name).strip()
            names = []
            for t in (data.get("tools") or []):
                fn = (t or {}).get("function") or {}
                n = str(fn.get("name") or "").strip()
                if n:
                    names.append(n)
            out[name] = names
    except Exception:
        out = {}
    with _LOCK:
        _SKILL_MAP_CACHE["key"] = key
        _SKILL_MAP_CACHE["map"] = out
    return out


def all_skill_tools() -> set[str]:
    """全部技能工具名集合（用于区分「技能工具」与「基础工具」）。"""
    s: set[str] = set()
    for names in skill_tool_map().values():
        s.update(names)
    return s


def extra_skill_tool_specs(skills: list[str], all_when_empty: bool = False) -> list[dict]:
    """把白名单技能的完整工具定义直接读出来。

    为什么必须自己读：渐进披露开启时，未激活技能的工具不进 collect_tool_specs()，
    子智能体拿不到它们的 schema（skill_help 的动态注册只作用于主智能体）。
    不补这一步，「技能白名单」就会变成「把技能禁用」，与档案本意完全相反。
    """
    out: list[dict] = []
    names = list(skills or [])
    if not names and all_when_empty:
        names = list(skill_tool_map().keys())      # 空=不限 → 全部启用技能
    for name in names:
        mf = SKILLS_DIR / str(name) / "skill.json"
        if not mf.exists():
            continue
        try:
            with open(mf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for t in (data.get("tools") or []):
            if isinstance(t, dict) and (t.get("function") or {}).get("name"):
                out.append(t)
    return out


# ── 档案 → 运行时行为 ─────────────────────────────────────
def system_prompt_for(profile: dict | None) -> str:
    if not profile:
        return ""
    return str(profile.get("system_prompt") or "").strip()[:MAX_PROMPT]


def active_skills_for(profile: dict | None) -> list[str]:
    """这个档案实际可用的技能。

    空列表 = 不限（全部技能，调用方按「全能力」处理）；
    非空 = 白名单 + ALWAYS_SKILLS（基本生存能力永远在）。
    """
    if not profile:
        return []
    skills = [str(x) for x in (profile.get("skills") or []) if str(x).strip()]
    if not skills:
        return []
    out: list[str] = []
    for s in list(skills) + list(ALWAYS_SKILLS):
        if s not in out:
            out.append(s)
    return out


def _denied(name: str, deny: list[str]) -> bool:
    for pat in deny:
        if pat == name or (("*" in pat or "?" in pat) and fnmatch.fnmatch(name, pat)):
            return True
    return False


def resolve_tools(profile: dict | None, all_tools: list[dict]) -> list[dict]:
    """按档案过滤工具定义。profile 为空 → 原样返回（与旧行为完全一致）。"""
    if not profile:
        return all_tools
    deny = [str(x) for x in (profile.get("deny_tools") or [])]
    allow = {str(x) for x in (profile.get("tools") or [])}
    skills = active_skills_for(profile)
    smap = skill_tool_map()
    skill_tools = all_skill_tools()
    allowed_skill_tools: set[str] = set()
    for s in list(skills) + list(ALWAYS_SKILLS):
        allowed_skill_tools.update(smap.get(s) or [])
    out = []
    for t in all_tools:
        name = str(((t or {}).get("function") or {}).get("name") or "").strip()
        if not name or _denied(name, deny):
            continue
        if allow:
            if name in allow:
                out.append(t)
            continue
        if skills and name in skill_tools and name not in allowed_skill_tools:
            continue      # 技能工具：只留白名单技能的
        out.append(t)     # 基础工具 / 白名单技能工具 → 保留
    return out


def describe(profile: dict | None) -> str:
    """档案属性摘要（不含 id/name——由调用方拼接，避免「`coder` 核心开发｜`coder` 核心开发」）。"""
    if not profile:
        return "（未指定档案：全能力通用执行者）"
    bits: list[str] = []
    eff = active_skills_for(profile)
    if eff:
        bits.append("技能=" + ",".join(eff))
    else:
        bits.append("技能=全部")
    if profile.get("tools"):
        bits.append(f"工具白名单={len(profile['tools'])} 个")
    if profile.get("deny_tools"):
        bits.append("禁用=" + ",".join(profile["deny_tools"]))
    if profile.get("model"):
        bits.append("模型=" + profile["model"])
    return "｜".join(bits)


def recommend_for_kind(kind: str) -> str:
    """按模块类型推荐档案 id（找不到就退回 general）。"""
    pid = KIND_PROFILE.get(str(kind or "").strip().lower(), "general")
    return pid if get_profile(pid) else "general"
