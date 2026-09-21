"""Plan Mode —— 只规划不动手（对标 codex 的 collaboration mode: plan）。

codex 把「先想清楚再动手」做成了协作模式：进入后只许非破坏性探索，最后产出
<proposed_plan>，由用户决定退出执行。它的流程写在提示词里
（codex-rs/collaboration-mode-templates/templates/plan.md），工具层只加了一道硬门禁：
Plan mode 下调 update_plan 直接报错（codex-rs/core/src/tools/handlers/plan.rs:87-91）。

大白的做法把门禁做实：状态落盘 + 执行前闸门拒掉一切改动型工具。
提示词只负责讲清三阶段流程，且只注入易变尾巴（不污染静态前缀缓存）。

与 plan_* 的分工（codex plan.md 第 11-15 行专门划清过）：
- plan_* 是工作清单/进度，Plan Mode 下不写它（codex 同样拒绝 update_plan）；
- Plan Mode 是协作模式：只探索、只提问，产出方案，不落任何改动。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

DEFAULT_UID = "default"

# 只读白名单：命中才放行。存疑一律拦（漏拦 = 模式被绕过，误拦 = 模型被拒后自己退出去）。
_READ_ONLY_EXACT = frozenset({
    "skill_help", "symbols", "read_json", "read_lines", "search_text", "list_files",
    "find_file", "system_check", "plan_show", "plan_history",
    "todo_list", "todo_get", "todo_plan",
    "harness_task_status", "harness_task_list",
    "workspace_get", "workspace_roots", "workspace_list", "workspaces_list",
    "linux_senses", "linux_audit", "linux_net", "linux_storage", "linux_guard",
    "linux_media", "linux_process", "linux_service",
    "plan_mode",  # 开关自身必须永远可达：拦了它就进了出不来
})

# code_verify/code_test/code_smoke 会执行代码，但 plan.md 明确允许 tests/builds
# （只要不改仓库跟踪的文件）；code_review 纯读 diff。
_READ_ONLY_PREFIX = (
    "code_search", "code_read", "code_locate", "code_analyze", "code_deps",
    "code_map", "code_list_files", "code_review", "code_verify", "code_test", "code_smoke",
    "code_git_status", "code_git_diff", "code_git_log", "code_git_blame",
    "sys_find", "sys_recent", "sys_locate",
)

_lock = threading.RLock()

# 忘了退出就永远只读——比误退出的代价大。超时后闸门自动解除，并留一条一次性提示。
# ttl <= 0 表示不超时（测试用）。
TTL_SECONDS = 2 * 3600
NOTICE_MAX_SHOWS = 2

INSTRUCTIONS = (
    "【Plan Mode（只规划不动手）】\n"
    "你现在处于 Plan Mode，直到用户明确让你退出。用户在这期间说「去做/直接改」"
    "只算「把执行也规划清楚」，不是开工信号；但系统提示里出现「闸门已解除」时不适用——"
    "那是用户已批准执行，直接开工，不要再规划。\n"
    "三条硬约束（执行前闸门强制，绕不过去）：\n"
    "1. 只读探索，不做改动——写文件/改代码/跑有副作用的命令会被直接拒；\n"
    "2. 先探索再提问——能从代码/配置/系统里查到的事实绝不许拿去问用户；"
    "只有「偏好与取舍」（查不到的）才问，直接在正文里问，"
    "给 2-4 个互斥选项并标注推荐默认，一次问清；\n"
    "3. 方案不完整不许交付——必须 decision complete：拿到它的人不需要再做任何决定。\n"
    "三阶段（别跳步）：\n"
    "· 阶段1 落地事实：先做一轮只读探索（代码/配置/入口/类型），把能从环境查到的未知消掉；\n"
    "· 阶段2 意图对话：问到能说清目标+验收标准、受众、范围内外、约束、现状、关键取舍；\n"
    "· 阶段3 实现对话：问到 decision complete——做法、接口（API/schema/IO）、数据流、"
    "边界与失败模式、测试与验收、上线与迁移。\n"
    "收尾：方案用 <proposed_plan> 块输出（标签各独占一行，内部 Markdown），"
    "3-5 段（概要/关键改动/测试计划/假设），只讲必要细节，别按文件清单罗列；"
    "一轮最多一个块；不要问「要不要我继续」。"
)


def _uid() -> str:
    try:
        import user_store

        return str(user_store.current_uid() or DEFAULT_UID)
    except Exception:
        return DEFAULT_UID


def _path() -> str:
    try:
        import user_store

        uid = user_store.current_uid()
        if uid:
            d = user_store.scoped_dir("plan_mode", uid)
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "state.json")
    except Exception:
        pass
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "plan_mode.json")


def _load() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    p = _path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def state() -> dict:
    """当前用户/会话的 Plan Mode 记录（无记录 = 从未进入）。"""
    return _load().get(_uid()) or {}


def _ttl_of(st: dict) -> float:
    v = st.get("ttl")
    return float(TTL_SECONDS) if v is None else float(v)


def _expired(st: dict) -> bool:
    ttl = _ttl_of(st)
    if ttl <= 0:
        return False
    return (time.time() - float(st.get("at") or 0)) > ttl


def _auto_leave(st: dict) -> None:
    """超时退出：解除闸门 + 留提示。闸门解除必须让模型知道，否则它会继续按只读办事。"""
    with _lock:
        data = _load()
        uid = _uid()
        cur = data.get(uid) or {}
        if not cur.get("active"):
            return
        cur["active"] = False
        cur["auto_left_at"] = time.time()
        cur["notice"] = (
            f"Plan Mode 已超时自动退出（停留超过 {int(_ttl_of(cur) // 60)} 分钟），"
            "改动型工具已恢复可用。要继续只规划不动手就重新 enter。"
        )
        data[uid] = cur
        _save(data)


def is_active() -> bool:
    st = state()
    if not st.get("active"):
        return False
    if _expired(st):
        _auto_leave(st)
        return False
    return True


def enter(topic: str = "", ttl_minutes: float | None = None) -> str:
    ttl = float(TTL_SECONDS) if ttl_minutes is None else float(ttl_minutes) * 60
    with _lock:
        data = _load()
        data[_uid()] = {
            "active": True,
            "topic": str(topic or "").strip(),
            "at": time.time(),
            "ttl": ttl,
        }
        _save(data)
    t = state().get("topic") or ""
    return (
        "已进入 Plan Mode：只做只读探索，改动型工具会被闸门拒绝。"
        + (f"\n主题：{t}" if t else "")
        + (f"\n超时保护：停留超过 {int(ttl // 60)} 分钟会自动退出。" if ttl > 0 else "")
        + "\n下一步：先做一轮只读探索，把能从代码/配置里查到的事实查清，再谈取舍。"
        "\n收尾时用 <proposed_plan> 块交出 decision complete 的方案。"
    )


def leave() -> str:
    with _lock:
        data = _load()
        prev = data.pop(_uid(), None) or {}
        _save(data)
    if not prev.get("active"):
        return "当前本来就不在 Plan Mode。"
    mins = int((time.time() - float(prev.get("at") or 0)) // 60)
    return f"已退出 Plan Mode（停留约 {mins} 分钟），可以动手执行了。"


def status_text() -> str:
    st = state()
    if not st.get("active"):
        return ("当前不在 Plan Mode（正常执行模式）。"
                "\n需要「先想清楚再动手」时，用 plan_mode(action=\"enter\") 进入。")
    t = st.get("topic") or ""
    mins = int((time.time() - float(st.get("at") or 0)) // 60)
    ttl = _ttl_of(st)
    left = f"，距自动退出约 {max(0, int(ttl // 60) - mins)} 分钟" if ttl > 0 else ""
    return (
        "Plan Mode 进行中"
        + (f"（主题：{t}）" if t else "")
        + f"，已停留约 {mins} 分钟{left}。"
        "\n此模式下只读探索放行，改动型工具被拒；收尾用 <proposed_plan> 块交出方案，"
        "要动手先 plan_mode(action=\"exit\")。"
    )


# ---- 用户措辞自动进出 ----
# 「把能力写进提示词」已被证明没用（经验库），所以进出必须挂在流程上：
# agent.chat_stream 拿到真实用户输入后调 auto_react。
# 进入是限制性的——误触发会拦掉用户想要的改动，所以只认高置信度措辞（动词必填）；
# 退出是解除限制，误触发代价小，判定可以宽松。两者都只在对应状态下才判。
_ENTER_RE = re.compile(
    # 动词必填，名词前允许少量修饰词（「先给我出个重构方案」）。修饰段里不许出现
    # 观察类动词：否则「先给我看看方案文件」会经「给」+「我看看」回溯命中，误拦改动。
    r"先(给我|帮我|你)?(出|写|做|给|来|拟)(一份|一个|个)?(?:(?!看|查|问|说|找|读)[^\s，,。.]){0,8}?(方案|计划|规划|设计|草案)"
    r"|先别(动手|动|改|写代码)"
    r"|(先|暂)不要(动手|动|改)"
    r"|别(动手|改代码)"
    r"|先想清楚|想清楚再(动手|改|做)"
    r"|只(要|出|给)(方案|规划|计划)"
    r"|先规划|先做规划|先设计"
)
_EXIT_RE = re.compile(
    r"批准|同意(执行|方案|开工|动手)|可以(执行|开工|动手)"
    r"|开始执行|执行方案"
    r"|按(这个|该|上述)?(方案)?(来|做|执行|干|办)"
    r"|开始(改|做|干|动手|写)|动手吧|开工|执行吧|干吧|去做吧|改吧"
    r"|go ahead|proceed|execute it",
    re.IGNORECASE,
)
# 自动进入的停留上限：用户走开时不该让改动型工具被拦两小时。
AUTO_TTL_MINUTES = 45


def detect_intent(text: str) -> str | None:
    """从用户原话判定该自动进入还是退出；判不出返回 None。

    只在对应状态下判：没进时「开始执行」不该触发任何动作，进了才当开工信号。
    """
    t = " ".join(str(text or "").split())
    if not t:
        return None
    if is_active():
        return "exit" if _EXIT_RE.search(t) else None
    return "enter" if _ENTER_RE.search(t) else None


def exit_by_user() -> str:
    """用户明确要求开工：退出并留一次性提示。

    与 leave() 的区别在提示：模型还在同一上下文里，不知道闸门已开就会继续按只读办事。
    """
    with _lock:
        data = _load()
        uid = _uid()
        cur = data.get(uid) or {}
        if not cur.get("active"):
            return "当前本来就不在 Plan Mode。"
        cur["active"] = False
        cur["notice"] = (
            "用户已批准/要求开工，Plan Mode 闸门已解除——改动型工具现在可用，按交出的方案执行。"
        )
        data[uid] = cur
        _save(data)
    return "已退出 Plan Mode。"


def auto_react(text: str) -> str | None:
    """按用户原话自动进出，返回给用户看的一句话（未触发返回 None）。

    调用方负责把这句透出到对话流：静默改状态会让用户以为工具坏了。
    """
    act = detect_intent(text)
    if act == "enter":
        topic = " ".join(str(text or "").split())[:60]
        enter(topic, AUTO_TTL_MINUTES)
        return (
            f"【已进入 Plan Mode】你要求先出方案，我先只做只读探索、不动任何东西"
            f"（{AUTO_TTL_MINUTES} 分钟无操作自动退出）。想直接开工说一句「批准/开始执行」即可。"
        )
    if act == "exit":
        exit_by_user()
        return "【已退出 Plan Mode】开工，改动型工具恢复可用。"
    return None


def _read_only(tool_name: str) -> bool:
    n = str(tool_name or "").strip()
    if not n:
        return False
    if n in _READ_ONLY_EXACT:
        return True
    return n.startswith(_READ_ONLY_PREFIX)


def check(tool_name: str) -> str | None:
    """执行前闸门：Plan Mode 下拒绝改动型工具，返回拒绝原因；None = 放行。"""
    if not is_active():
        return None
    if _read_only(tool_name):
        return None
    return (
        f"Plan Mode 拒绝：{tool_name} 会改动状态，当前只允许只读探索。"
        f"要么换只读工具（code_read/code_search/code_git_diff/read_json 等）把方案查清，"
        f"要么先 plan_mode(action=\"exit\") 退出再动手。"
        f"想收尾就先把方案写成 <proposed_plan> 块交给用户。"
    )


def prompt_block() -> str:
    """易变尾巴用的提示词段；未激活时返回待送达的一次性提示（超时退出等）。"""
    if is_active():
        return INSTRUCTIONS
    return take_notice()


def take_notice() -> str:
    """取一次性提示，送达 NOTICE_MAX_SHOWS 次后自清（避免每轮刷屏）。"""
    with _lock:
        data = _load()
        uid = _uid()
        cur = data.get(uid) or {}
        n = str(cur.get("notice") or "")
        if not n:
            return ""
        shown = int(cur.get("notice_shown") or 0) + 1
        if shown >= NOTICE_MAX_SHOWS:
            cur.pop("notice", None)
        cur["notice_shown"] = shown
        data[uid] = cur
        _save(data)
    return "【Plan Mode 提示】" + n
