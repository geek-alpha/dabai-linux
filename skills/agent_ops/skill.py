"""智能体指挥技能 —— 委派任务给 DSH/Codex/OpenCode，查询任务中心进展，技能工坊。

与原来 agent.py 的 execute_local_tool 实现完全一致：
- dsh 委派返回 __dsh_bridge__ JSON → server 登记待确认任务（前端弹确认卡片）；
- codex/opencode 委派返回 __codex_delegate__ JSON → server 同样先登记待确认任务，
  用户确认后才真正执行（安全闸门与 DSH 一致，防误操作）；
- list_agent_tasks 直接查询任务中心并格式化返回。
另兼容旧工具名 alias：call_deepseek_harness / delegate_codex_task。

合并自原 3 个技能：
- agent_ops（智能体指挥：委派/任务中心）
- sub_agents（通用子智能体下发/查看/收回 4 工具）
- skill_dev（技能工坊：自建自改技能 + 全网拉取技能）
"""
from __future__ import annotations

import json
import os
import sys

# 合并自原 sub_agents 技能：通用子智能体下发/查看/收回 4 个工具
# 合并自原 skill_dev 技能：技能工坊 11 个工具
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)
import sub_agents_impl  # noqa: E402
import agent_profiles_impl  # noqa: E402
import skill_dev_impl  # noqa: E402
import skill_pull_impl  # noqa: E402


def delegate(args: dict, tool_name: str = "delegate_agent_task") -> str:
    task = str(args.get("task") or "").strip()
    if not task:
        return "请提供要交给智能体的任务内容（task 参数不能为空）"
    cwd = str(args.get("cwd") or "").strip() or None
    agent = str(args.get("agent") or "").lower().strip()
    if tool_name == "call_deepseek_harness" or agent == "dsh":
        return json.dumps({"__dsh_bridge__": True, "task": task, "cwd": cwd},
                          ensure_ascii=False)
    if tool_name == "delegate_codex_task":
        tool = str(args.get("tool") or "").lower()
        agent = {"cx": "codex", "ai": "opencode"}.get(tool, tool)
    if agent not in ("codex", "opencode"):
        return ("agent 参数必须是 dsh、codex 或 opencode（三个都由 LLM 分流平等选择）："
                "dsh=DSH 智能体（DeepSeek Harness）——解决复杂任务的好帮手，"
                "适合跨系统、多步骤、需要深入调查的任务；"
                "codex——攻坚顶级难题，适合算法难题/复杂重构/棘手 bug/性能优化；"
                "opencode——解决日常问题，适合日常小需求/快速改文件/写脚本/跑测试。"
                "用户明确点名用 DSH / DeepSeek Harness 时必须选 dsh。"
                "三个智能体（dsh/codex/opencode）地位平等、都可调用，"
                "所有委派执行前都会先请用户确认，防误操作。")
    return json.dumps({"__codex_delegate__": True, "agent": agent, "task": task},
                      ensure_ascii=False)


def list_tasks(args: dict) -> str:
    try:
        from task_orchestrator import get_orchestrator, AGENTS
        tasks = get_orchestrator().list(20)
    except Exception as e:
        return f"任务中心暂不可用: {e}"
    if not tasks:
        return "当前任务中心没有任务，一切安静。"
    lines = []
    for t_ in tasks:
        st = t_.get("status", "")
        mark = {"running": "🔄 执行中", "confirming": "⏳ 待确认",
                "queued": "⏳ 排队中", "done": "✅ 完成",
                "error": "❌ 失败", "cancelled": "🛑 已取消"}.get(st, st)
        title = t_.get("title", "")
        channel = t_.get("channel", "")
        agent_meta = t_.get("agent") or {}
        agent_name = agent_meta.get("name") or channel or ""
        result_tail = (t_.get("result") or "")[:80]
        lines.append(f"- [{mark}] {title}（执行者：{agent_name}）"
                     + (f"\n    结果: {result_tail}" if result_tail else ""))
    return "当前任务中心：\n" + "\n".join(lines)


def delegate_task(args: dict) -> str:
    return delegate(args, "delegate_agent_task")


def delegate_alias_dsh(args: dict) -> str:
    return delegate(args, "call_deepseek_harness")


def delegate_alias_codex(args: dict) -> str:
    return delegate(args, "delegate_codex_task")


HANDLERS = {
    "delegate_agent_task": delegate_task,
    "list_agent_tasks": list_tasks,
    # ---- 合并自原 sub_agents 技能（4 个通用子智能体工具）----
    "sub_agent_spawn": sub_agents_impl.sub_agent_spawn,
    "sub_agents_list": sub_agents_impl.sub_agents_list,
    "sub_agent_status": sub_agents_impl.sub_agent_status,
    "sub_agent_cancel": sub_agents_impl.sub_agent_cancel,
    # ---- 智能体档案（配置子智能体：人格/技能/工具/模型）----
    "agent_profile_list": agent_profiles_impl.agent_profile_list,
    "agent_profile_show": agent_profiles_impl.agent_profile_show,
    "agent_profile_create": agent_profiles_impl.agent_profile_create,
    "agent_profile_update": agent_profiles_impl.agent_profile_update,
    "agent_profile_delete": agent_profiles_impl.agent_profile_delete,
    # ---- 合并自原 skill_dev 技能（技能工坊 8 工具）----
    "skill_dev_list": skill_dev_impl.skill_dev_list,
    "skill_dev_read": skill_dev_impl.skill_dev_read,
    "skill_dev_create": skill_dev_impl.skill_dev_create,
    "skill_dev_edit": skill_dev_impl.skill_dev_edit,
    "skill_dev_write_file": skill_dev_impl.skill_dev_write_file,
    "skill_dev_validate": skill_dev_impl.skill_dev_validate,
    "skill_dev_reload": skill_dev_impl.skill_dev_reload,
    "skill_dev_remove": skill_dev_impl.skill_dev_remove,
    # ---- 合并自原 skill_pull 技能（全网拉取 3 工具）----
    "skill_pull_search": skill_pull_impl.skill_pull_search,
    "skill_pull_inspect": skill_pull_impl.skill_pull_inspect,
    "skill_pull_install": skill_pull_impl.skill_pull_install,
}
