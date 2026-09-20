"""工具级确认闸门：把「该问不该问」纪律代码化。

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
from typing import Any, Dict, FrozenSet, Tuple

logger = logging.getLogger("tool_gate")

# ---------- 高危工具：不可逆/越界/花钱/委派/自动执行 ----------
# 判定依据（对应 agent 里的「该问不该问」）：
#   ①动作不可逆（删除/覆盖/重启/发布）；②越出被点名范围；③代价差一个量级。
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
        if allowed is not None and sig in allowed:
            return "allow", "", sig
        if pending is not None and sig in pending:
            return "ask", "已在你面前弹出确认卡片，等待你决定", sig
        if _is_readonly(tool_name):
            return "allow", "", sig
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
