"""工具级确认闸门：把「授权与自主」闸门纪律代码化。

层级的判据只有一条——这个动作是否不可逆或越出信任边界：

  只读工具（不改变磁盘任何状态）         → 零确认放行，绝不打断
  常规写操作（改自己工作区文件/跑测试）   → 信任边界内自由执行
  不可逆/越界/花钱/委派/自动执行         → 要求用户确认（ask）
  用户已拒绝过的同一动作（签名命中）      → 直接拒绝（deny），不再骚扰

fail-closed：评估器自身出异常时，只读工具放行、写工具按 ask 处理
（宁可等确认，也不误跑一个没把握的写操作）。确认记忆是会话级的：
重启即清空——再从用户那里求一次授权，方向安全。

签名 = 规范化参数后的确定性标识（工具名 + 参数 JSON），同一动作重复
出现（模型重试/用户允许后重调）可精确识别，白名单与黑名单都用它。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, FrozenSet, Tuple

logger = logging.getLogger("tool_gate")

BASE_DIR = Path(__file__).parent.resolve()
_SETTINGS_PATH = BASE_DIR / "settings.json"


def perm_key(tool_name: str, arguments: dict) -> str:
    """永久白名单键：按『高危判定单元』而非签名——同一工具同一危险模式归一类。

    用户点『总是允许』的语义是『这类操作以后别再问』，不是『这一次放行』：
    rm -rf 的签名随参数变，但危险模式只有一个。三类：
      tool:<名>      HIGHRISK_TOOLS 命中的整工具
      shell:<i>      第 i 条危险 shell 模式（含 rm -rf / git push -f 等）
      overwrite:<名> 带覆盖/强推参数的工具
    未命中任何高危 → 返回空串（不需要永久白名单）。
    """
    if tool_name in HIGHRISK_TOOLS:
        return f"tool:{tool_name}"
    if tool_name in ("shell_run", "shell", "exec", "sh"):
        cmd = _shell_arg(arguments)
        for i, pat in enumerate(_HIGHRISK_SHELL_PATTERNS):
            if pat.search(cmd):
                return f"shell:{i}"
    if tool_name == "code_create_file" and any(
            bool(arguments.get(k)) for k in _OVERWRITE_ARGS):
        return f"overwrite:{tool_name}"
    return ""


def load_permanent() -> dict:
    """读 settings.json 的 tool_gate.permanent_allow；缺失/损坏返回 {}。"""
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return dict((cfg.get("tool_gate") or {}).get("permanent_allow") or {})
    except Exception as e:
        logger.warning("永久白名单读取失败: %s", e)
        return {}


def save_permanent(key: str, tool_name: str, reason: str) -> bool:
    """把一条永久白名单写进 settings.json（读改写，保留其它配置）。失败返回 False。"""
    try:
        if _SETTINGS_PATH.exists():
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {}  # 新部署无 settings.json：按空配置直接建，不能静默失败
        tg = cfg.setdefault("tool_gate", {})
        allow = tg.setdefault("permanent_allow", {})
        allow[key] = {
            "tool": tool_name,
            "reason": reason[:60],
            "at": int(time.time()),
        }
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning("永久白名单写入失败: %s (%s)", key, e)
        return False
def list_permanent() -> list:
    """永久白名单全量清单（按加入时间倒序），给管理面板展示。"""
    raw = load_permanent()
    items = []
    for key, meta in raw.items():
        if not isinstance(meta, dict):
            meta = {}
        items.append({
            "key": key,
            "tool": str(meta.get("tool") or ""),
            "reason": str(meta.get("reason") or ""),
            "at": int(meta.get("at") or 0),
            "desc": _perm_desc(key, meta),
        })
    items.sort(key=lambda i: i["at"], reverse=True)
    return items


def remove_permanent(key: str) -> bool:
    """从永久白名单撤销一条（settings.json 读改写，保留其它配置）。"""
    try:
        if not _SETTINGS_PATH.exists():
            return False
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        tg = cfg.get("tool_gate") or {}
        allow = tg.get("permanent_allow") or {}
        if key not in allow:
            return False
        del allow[key]
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("永久白名单已撤销: %s", key)
        return True
    except Exception as e:
        logger.warning("永久白名单撤销失败: %s (%s)", key, e)
        return False


def _perm_desc(key: str, meta: dict) -> str:
    """把 perm_key / 元信息翻译成人话，给管理面板的行内说明。"""
    tool = str(meta.get("tool") or "")
    if key.startswith("tool:"):
        return f"工具 {tool}：同类不可逆操作不再询问"
    if key.startswith("shell:"):
        return f"shell 命令命中第 {key.split(':', 1)[1]} 条危险模式：不再询问"
    if key.startswith("overwrite:"):
        return f"工具 {tool} 带覆盖/强推参数：不再询问"
    return f"永久信任 {tool or key}"


# ---------- 高危工具：不可逆/越界/花钱/委派/自动执行 ----------
# 判定依据（对应 agent 里的【授权与自主】闸门）：
# 没把握是不是高危的，就不放进来——闸门宁少拦，也别把日常写操作全堵死。
HIGHRISK_TOOLS: FrozenSet[str] = frozenset({
    # 删除类（不可逆）
    "todo_delete", "sched_remove", "wt_discard",
    # 委派/后台长任务/定时自动执行（占资源、可能花钱）
    "delegate_agent_task", "harness_flow_submit", "harness_flow_dsl_submit",
    "harness_batch_submit", "sched_add",
    # 对用户可见的对外动作（发消息/发通知）
    "send_message", "sched_run_now",
})

# ---------- shell 危险模式：命令里出现即 ask ----------
# 只拦「磁盘级不可逆」——rm -rf、强制重置、格式化、覆盖设备。
# 日常的 ls/cd/git commit/编译测试全放行。
_HIGHRISK_SHELL_PATTERNS = (
    # 注意：不能写成 \b(-rf|...) —— \b 要求 - 前是词边界，但参数以 - 开头
    # 时边界在 '-' 之前，\b 永远配不上。用「命令字 + 非贪婪参数 + 字面标志」：
    re.compile(r"\brm\b[^;\n]*?(?:-rf|-r\s+-f|--recursive|--force)"),
    re.compile(r"\bgit\s+push\b[^;\n]*?(?:-f|--force)"),
    re.compile(r"\bgit\s+(?:reset|clean)\b[^;\n]*?(?:--hard|-f|--force)"),
    re.compile(r"\bmkfs\.?\w*(\s|$)"),
    re.compile(r"\bdd\b[^;\n]*?\bof=/(?:dev|api|proc)\b"),
    re.compile(r"\bchmod\b[^;\n]*?777\b"),
    re.compile(r"\bshred\b|\bwipefs\b|\bfdisk\b|\bparted\b"),
    re.compile(r":\(\)\s*\{\s*\|"),  # fork bomb
)

# 参数里带覆盖语义的工具（同工具不同参数按签名区分）
_OVERWRITE_ARGS = ("overwrite", "force", "confirm")


def signature(tool_name: str, arguments: dict) -> str:
    """动作签名：同一工具同一参数 → 同一签名（白/黑名单的键）。"""
    try:
        if isinstance(arguments, dict):
            raw = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        else:
            raw = str(arguments)
    except Exception:
        raw = str(arguments)
    return hashlib.sha1(f"{tool_name}|{raw}".encode("utf-8")).hexdigest()[:16]


def _shell_arg(arguments: dict) -> str:
    """从参数里提取可执行的命令文本（shell 类工具的命令参数名不一）。"""
    v = arguments.get("command")
    if v is None:
        for k in ("cmd", "exec", "script"):
            v = arguments.get(k)
            if v is not None:
                break
    return str(v or "")


def _ask_reason(tool_name: str, arguments: dict) -> str:
    """高危判定的原因，给前端确认卡 & 模型回填用。"""
    cmd = _shell_arg(arguments)
    for pat in _HIGHRISK_SHELL_PATTERNS:
        if pat.search(cmd):
            return f"命令含不可逆操作（{pat.pattern[:24]}…）"
    return f"工具 {tool_name} 属于不可逆/越界操作，需你确认"


def evaluate(
    tool_name: str,
    arguments: dict,
    allowed: Any = None,
    denied: Any = None,
    pending: Any = None,
    permanent: Any = None,
) -> Tuple[str, str, str]:
    """评估一次工具调用。

    Returns:
        (verdict, reason, sig)
        verdict: "allow" 直接执行 / "ask" 需用户确认 / "deny" 已被拒绝
        reason: 给用户看的说明（ask/deny 时）
        sig: 动作签名（白/黑名单的键，allow 时为空串不参与）
    """
    try:
        sig = signature(tool_name, arguments)
        if denied is not None and sig in denied:
            return "deny", "你已拒绝过这个操作", sig
        if permanent is not None:
            pk = perm_key(tool_name, arguments)
            if pk and pk in permanent:
                # 永久白名单：用户点过『总是允许』，整类操作直接放行
                return "allow", "", sig
        if allowed is not None and sig in allowed:
            return "allow", "", sig
        if pending is not None and sig in pending:
            return "ask", "已在你面前弹出确认卡片，等待你决定", sig
        if _is_readonly(tool_name):
            return "allow", "", sig
        # 生存压力咬到自己：余额见底时掐掉烧钱大户。排在只读之后 —— 读文件不花钱，
        # 穷的时候更该允许我先把情况看清楚。模块不在（还没升级）就是空串 = 老行为。
        try:
            import peer_ledger as _pl
            _broke = _pl.heavy_block(tool_name)
        except Exception:
            _broke = ""
        if _broke:
            return "deny", _broke, sig
        if tool_name in HIGHRISK_TOOLS:
            return "ask", _ask_reason(tool_name, arguments), sig
        if tool_name in ("shell_run", "shell", "exec", "sh"):
            cmd = _shell_arg(arguments)
            if any(p.search(cmd) for p in _HIGHRISK_SHELL_PATTERNS):
                return "ask", _ask_reason(tool_name, arguments), sig
        if tool_name == "code_create_file":
            # 覆盖已有文件是明确的不可逆覆盖；不传 overwrite 的创建放行
            if any(bool(arguments.get(k)) for k in _OVERWRITE_ARGS):
                return "ask", "创建文件带覆盖/强制参数", sig
        return "allow", "", sig
    except Exception as e:  # fail-closed：评估器坏了也不能误跑写操作
        logger.warning("tool_gate 评估异常，按 fail-closed 处理: %s", e)
        try:
            if _is_readonly(tool_name):
                return "allow", "", signature(tool_name, arguments)
        except Exception:
            pass
        return "ask", "安全评估不可用，暂不执行（fail-closed）", signature(tool_name, arguments)


def _is_readonly(tool_name: str) -> bool:
    try:
        from harness.tool_sched import is_readonly
        return is_readonly(tool_name)
    except Exception:
        return False
