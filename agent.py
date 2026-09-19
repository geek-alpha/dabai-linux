"""AI Agent 核心模块 —— 融合技能工具调用 + Function Calling 循环 + 长期记忆。

特性：
- 支持 OpenAI 兼容 API 的流式 function calling
- 工具全部 skill 化（本地工具 + harness 技能/插件，经 skill.json 注册）
- 支持本地工具（从 tools.json 加载）
- 工具调用多轮循环（默认不限制轮数，可经 settings.json -> agent.max_tool_rounds 设上限）
- 长期记忆：自动保存对话历史，检索相关上下文
- 向后兼容 web_agent 的 chat_stream_async 接口
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from memory import ChatMemory, estimate_tokens
from proxy_utils import is_local_url, resolve_llm_proxy
from tool_validation import find_tool_spec, validate_arguments

logger = logging.getLogger("agent")

BASE_DIR = Path(__file__).parent.resolve()

# 工具调用最大轮数（防止无限循环）；0 / 负数表示不限制，
# 可由 settings.json -> agent.max_tool_rounds 覆盖（见 _max_tool_rounds）
MAX_TOOL_ROUNDS = 0
# 死循环防护：连续 N 轮调用「完全相同工具+参数」才停止（默认 50，容错更高，
# 避免模型多轮重试同一工具时被误杀）；可由 settings.json -> agent.repeat_guard_rounds 覆盖
REPEAT_GUARD_LIMIT = 50
# 死循环防护（强判据）：连续 N 轮调用相同「且返回结果也完全相同」即停。
#
# 为什么需要两条判据：只看「调用相同」无法区分两种完全不同的情况——
#   · 合理轮询：同一个 video_status 调用，结果 10% → 30% → 60%（在推进！）
#   · 真死循环：同一个调用，结果一字不差地重复（在原地打转）
# 为了不误杀前者，原阈值只能抬到 50 轮——代价是真死循环要烧满 50 次 LLM
# 往返（每轮 2~6 秒）才停，几十秒到几分钟白等，token 也全烧掉。
# 把「结果」纳入判据后两个目标同时达成：结果在变 = 绝不拦；结果不变 = 4 轮就停。
SAME_RESULT_GUARD_LIMIT = 4
# 工具调用超时（秒）
TOOL_CALL_TIMEOUT = 30.0
# 工具执行心跳间隔（秒）：超过该间隔仍未结束时，向客户端推送 ToolCallProgress 进度事件
TOOL_HEARTBEAT_INTERVAL = 5.0
# 工具超时上限（秒）：这是天花板而不是实际值——只有 _TOOL_TIMEOUT_OVERRIDES /
# settings.json -> agent.tool_timeouts 点名的工具才放大到接近它，其余仍按
# TOOL_CALL_TIMEOUT(30s) 执行。2026-09-13 抬到 1200：shell_run 要能跑满 20 分钟
# 的长任务（全店诊断、批量发布），此前 300 一刀切把它砍在半路。默认短、点名长。
TOOL_CALL_TIMEOUT_MAX = 1200.0
# 长耗时工具的超时覆盖（秒）：默认 tool_call_timeout 对这些工具太短。
# 可在 settings.json -> agent.tool_timeouts 里按工具名继续覆盖。
# 支持「一次调用做多件事」的工具：{工具名: (批量参数名, 用法说明)}
# 用途：① 本轮同一工具被拆成多次调用时，给模型一次基于事实的反馈；② 统计可合并调用数。
# 只列**真的有批量参数**的工具——没有批量参数的工具（如 find_file）提示也无用。
_BATCHABLE_TOOLS = {
    "code_read": ("files", "它支持 files 参数一次读多个文件（逗号/换行分隔，每项可带 :起-止）"),
    "code_search": ("queries", "它支持 queries 参数一次搜多个关键词（数组或换行/逗号分隔）"),
}



_TOOL_TIMEOUT_OVERRIDES = {
    "pmx_to_vrm": 300.0,          # Blender 转换（内部 600s 子进程超时，主轮最多等 5 分钟）
    "shell_run": 1200.0,          # 长命令（全店诊断/批量发布）：给满 20 分钟，与 MAX 齐平
    "wt_run": 300.0,              # 工作树内跑命令
    "wt_create": 180.0,           # git worktree add 冷启动
    "anim_batch": 300.0,          # Mixamo 批量下载
    "anim_download": 240.0,       # 单动作下载 + 浏览器自动化
    "anim_optimize": 300.0,       # 动作优化/后处理
    "skill_pull_install": 300.0,  # GitHub 拉取 + 校验 + 安装
}

# 轮内工具历史压缩（2026-08-30）：工具循环里每轮执行完，旧轮的工具结果仍全量
# 留在 messages 中重发给模型——shell_run/code_read 单条可达 16000 字。DeepSeek V4
# 上下文是 1M（1049K），正常工程任务几十轮远不会爆窗，因此预算按窗口的 1/4
# （默认 256K）设置，只在极端超长轮次时兜底压缩；过早压缩反而逼模型重读文件、
# 增加工具轮数，得不偿失。压缩规则：
# - 最新 keep_rounds 轮保持完整（模型正在用的结果不砍）；
# - 更早轮次的工具结果截成片段（结构保留，不破坏 function-calling 校验）；
# - 总量仍超预算时再压缩最新一轮的结果。
# 可用 settings.json -> agent.mid_turn_max_tokens / tool_result_retro_cap 调整。
MID_TURN_MAX_TOKENS = 262144
TOOL_RESULT_RETRO_CAP = 250
ASSISTANT_RETRO_CAP = 150
KEEP_NEWEST_TOOL_ROUNDS = 1
# 单条工具结果的 token 上限（轮内压缩的第二条触发线，见 _single_result_max_tokens）。
SINGLE_RESULT_MAX_TOKENS = 5000
# 短期窗口的滞回上限（token）：ctx 打包出来的历史平时只追加，累计超它才一次性
# 截回一半。这是「每轮失效」→「每 N 轮失效一次」的唯一开关，见
# Agent._stable_history_messages 的实测数据。
HIST_VIEW_MAX_TOKENS = 16000
# 单轮内最多同时注册的工具定义数（渐进式披露的轮内上限）。
# 上限的用途是「别让工具 schema 无限膨胀」，不是「省一点常驻开销」：
# tools 排在请求最前，卸载一个技能 = 从数组中间删元素 → 其后全部重排 →
# 整条前缀（含全部历史）100% 失效。实测一次截断废掉 200k+ 字符（全价计费），
# 而多留一个技能的工具（code_ops 47 个 = 23.5k 字符）每轮只按缓存价计费——
# 一次截断 ≈ 白留 85 轮。所以上限必须远高于「实际会用到的技能总量」：
# 会话内让工具集只增不减（全部技能合计 66k 字符，200 个工具留足余量）。
MAX_ACTIVE_TOOLS = 200
# 工具 schema 总字符上限（个数相同、描述膨胀同样烧钱）：tools 按字符计价，
# 它又排在请求最前——所以「太重」得按字符量，而不是按工具个数。
# token 当量：tools schema 密度 0.393 token/字符（三个 i=0 锚点两两联立，一致
# 到 ±0.004），故 80k 字符 ≈ 31.4k token。实测模型 deepseek-flash 窗口 1M，
# 占比 3.1%——上限无需动。别拿轮内 i>0 的点解密度：messages 会被短期窗口截断，
# dc 虚高，解出的密度低到 0.18（真值 0.46）。
MAX_ACTIVE_TOOLS_CHARS = 80000
# 文本协议工具调用标记：本地模型（如 Ollama draganis/vanessa）不支持原生 function
# calling 时，通过系统提示词注入工具说明，模型以 <tool_call>{...}</tool_call> 标记发起调用。
# 兼容两种写法：<tool_call>{"name":...}</tool_call> 与 <tool_call {"name":...}>
TEXT_TOOL_CALL_RE = re.compile(r"<tool_call\s*>\s*(\{.*?\})\s*(?:</tool_call>|\s*>)", re.S)
# 解析用：定位 <tool_call ...> 到 </tool_call>（或内容结尾）之间的"参数区"，
# 再用 _extract_balanced_json 括号配平截取完整 JSON——任务文本里出现 { }（如"参考 {xx}/a.py"）
# 或长描述时，不再被非贪婪匹配提前掐断。
TEXT_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call\b[^>]*>(.*?)(?:</tool_call>|\s*$)", re.S)
# 展示用：剥离标记（含未闭合写法），避免任务 JSON 残留在聊天面板。
TEXT_TOOL_CALL_STRIP_RE = re.compile(
    r"<tool_call\b[^>]*>.*?</tool_call>|</?tool_call\b[^>]*>", re.S
)

# 由 server 端完整消费的"特殊标记 JSON"：委派 codex/opencode、DSH 桥接、屏幕命令。
# 其中 task 字段可能很长，一旦被下方 2000 字符截断，JSON 会失效或任务内容不完整，
# 因此这类结果必须原样保留、不做长度截断。
_SPECIAL_TOOL_RESULT_KEYS = ("__codex_delegate__", "__dsh_bridge__", "__screen_command__")


def _is_special_tool_result(result: str) -> bool:
    """判断工具结果是否为 server 端消费的特殊标记 JSON。"""
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return (isinstance(data, dict)
            and any(k in data for k in _SPECIAL_TOOL_RESULT_KEYS))
@dataclass
class ChatTurnContext:
    """一次对话轮的全部上下文（打包 15 个散参数，消除长参数列表坏味道）。

    由公共入口 chat_stream 构造，透传给 _chat_stream_inner /
    _chat_stream_normal。函数体开头解包为局部变量，
    行为与旧签名完全等价。
    """
    message: str
    history: list = None
    enable_tools: bool = True
    current_model: Optional[str] = None
    current_background: Optional[str] = None
    current_bgm: Optional[str] = None
    msg_source: str = "chat"
    current_anim: Optional[dict] = None
    turn_id: Optional[str] = None
    resume: Optional[dict] = None
    record_history: bool = True
    proactive: bool = False


# 工具结果回传模型/前端时的长度策略：
# - 普通工具默认上限 2000 字符（防刷屏）；
# - 代码/技能类工具是「查代码、看 diff、读文件」的主战场，结果被砍短会逼着模型
#   反复重读、重查（损耗极高），因此单独放宽到 16000 字符；特殊标记 JSON 仍完整保留。
_TOOL_RESULT_LIMIT = 2000
_TOOL_RESULT_LIMIT_LONG = 16000
_LONG_RESULT_TOOLS = frozenset((
    "code_read", "code_search", "code_locate", "code_analyze", "code_deps",
    "code_list_files", "code_edit", "code_patch", "code_create_file",
    "code_append",
    "code_verify", "code_test", "code_review",
    "code_git_status", "code_git_diff", "code_git_log", "code_git_blame",
    "skill_dev_read", "skill_dev_list", "skill_dev_validate", "skill_dev_reload",
    "skill_dev_write_file", "skill_dev_create", "skill_dev_edit",
    "skill_help", "shell_run",
    "read_lines", "search_text", "git_diff", "git_status", "read_json",
    "find_file", "list_files", "system_check", "symbols",
))


# 截断时保留的尾部长度。为什么必须留尾巴：code_read 的「（还有 N 行未显示：
# 可用 x.py:156-195 继续读）」正好在结果末尾，一刀砍掉尾部等于把「怎么拿到
# 剩下的内容」这条唯一提示也砍了。
_TRUNC_TAIL = 600
# 给截断提示语预留的长度（提示语本身要占位置，不能挤爆上限）
_TRUNC_NOTICE_RESERVE = 220


def _hist_msg_key(m: dict) -> tuple:
    """历史消息的「抗截断」身份（用法见 AIAgent._stable_history_messages）。

    不用全文：最新一轮不截字符、变旧后会被字符上限截断，同一轮「由新变旧」
    全文必然不同——拿全文当锚点会把「正常变旧」误判成「换会话」。
    """
    tcs = m.get("tool_calls")
    return (str(m.get("role") or ""), str(m.get("content") or "")[:80],
            len(tcs) if isinstance(tcs, list) else 0)


def _trace_tag() -> str:
    """诊断日志的来源标记：run=真实运行，test=测试/探针。

    为什么必须有：自检脚本和探针会把合成样本写进同一个日志文件，
    混在一起就分不清「真实运行长什么样」——这次排查就卡在这：
    48 条样本里分不出哪条是现场、哪条是测试造的。
    """
    return os.environ.get("DABAI_TRACE_TAG") or "run"


def _trace_hist_view(sid, packed_n, prev_n, anchor, mode, view_n, inst="", prev_sid="") -> None:
    """滞回视图诊断：每轮追加一行，回答「真实运行是只追加还是每轮重建」。

    为什么要它：tools/hist_view_replay.py 用真实消息库重放，测出锚点从不丢失、
    重建全部来自 trim（约每 7 轮一次）；但 tools/cold_audit.py 的真实运行数据里，
    断点几乎每轮都落在 history[0]（可复用只剩 sys+memory ≈ 2.4k）。两者矛盾，
    只能让运行现场自己说话——观测代码不参与消息构造，写失败也静默忽略。

    为什么要 inst/pid：真实样本里 reset_sid 反复出现，而 sid 明明没变
    （05:08:15 与 05:09:06 都是 73e3be897d53，prev 却是 0）。
    「同一会话却当新会话重建」只有两种可能：视图被清、或根本不是同一个对象。
    没有实例标识就只能靠猜——带上 inst（id 后 4 位）与 pid，一眼分辨：
    inst 变了 = 换了实例（每轮新建/进程重启）；inst 没变 = 视图真被清了。

    为什么要 psid（上一轮记下的 sid）：只记当前 sid 时，「sid 到底变没变」看不见——
    两行 reset_sid 的 sid 都是同一个值，那是「当前值」，不是「判定依据」。
    补上上一轮的 sid，reset_sid 就能自证原因：
      psid 空   = 视图从未建立（新实例 / 进程重启）→ 正常；
      psid≠sid  = 会话真切换了 → 预期行为；
      psid=sid  = 同会话却把视图清了 → 真 bug，只有这种才需要修。
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "hist_view_trace.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 1),
                                "sid": str(sid or "")[:12], "packed": packed_n,
                                "prev": prev_n, "anchor": anchor, "mode": mode,
                                "view": view_n, "inst": inst, "pid": os.getpid(),
                                "psid": str(prev_sid or "")[:12],
                                "tag": _trace_tag()},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass



def _hist_msg_tokens(m: dict) -> int:
    """单条历史消息的 token 估算：content + tool_calls JSON（结构同样计费）。"""
    n = estimate_tokens(str(m.get("content") or ""))
    if m.get("tool_calls"):
        n += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
    return n


def _hist_view_tokens(msgs: list) -> int:
    return sum(_hist_msg_tokens(m) for m in (msgs or []))


def _trim_hist_view(msgs: list, target_tokens: int) -> list:
    """从旧端按「整轮」裁到 target_tokens 以内。

    切点必须落在 user 消息上：切在中间会把 tool_call 与它的结果拆散，
    悬空引用会让 function-calling 校验失败（比缓存失效严重得多）。
    """
    per = [_hist_msg_tokens(m) for m in msgs]
    suffix = [0] * (len(msgs) + 1)
    for i in range(len(msgs) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + per[i]
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    # 升序扫描：suffix 随 i 递减，第一个「裁完不超预算」的切点 = 保留最多内容。
    # 倒序扫描是错的：第一个满足条件的总是最新的那个 user，会一刀砍到只剩一轮。
    for i in starts:
        if suffix[i] <= target_tokens:
            return msgs[i:]
    return msgs[starts[-1]:] if starts else msgs[-1:]


# ---- 历史视图的跨进程持久化 ----
# 视图是「进程内」状态，进程一重启就丢；丢了只能从打包结果重建，而打包结果的
# 头部每轮按 token 预算重挑，落点会变 → 断点回到 history[0] → 其后整段历史
# （含全部历史与尾巴）每轮白付一次全价。
# 实测（data/turn_metrics.jsonl × data/hist_view_trace.jsonl，46 轮）：
#   · instance_new → reset_sid 重建之后的头几轮，断点全在第 1~3 条，
#     单轮白烧 68k~324k 字符；而视图连续的轮次断点稳定在 history[45~53]，
#     只废 2k~16k——差两个数量级。
HIST_VIEW_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "hist_view_state.json")
# 存盘上限：只防「文件无限长大」，不参与消息构造。视图本身另有 token 上限
# （HIST_VIEW_MAX_TOKENS），这里给足余量。
HIST_VIEW_STATE_MAX_MSGS = 400


def _hist_view_persist_enabled() -> bool:
    """自检/探针（tag=test）不读写真实状态文件：合成样本绝不能污染现场。"""
    return _trace_tag() != "test"


def _load_hist_view(sid) -> list:
    """读回上一进程留下的视图。sid 不一致就当没有（防跨会话串上下文）。

    任何异常一律降级为「没有视图」——观测/优化代码绝不能弄挂主路径。
    """
    if not sid or not _hist_view_persist_enabled():
        return []
    try:
        with open(HIST_VIEW_STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if str(st.get("sid") or "") != str(sid):
            return []
        view = st.get("view")
        if not isinstance(view, list):
            return []
        return [m for m in view if isinstance(m, dict)]
    except Exception:
        return []


def _save_hist_view(sid, view) -> None:
    """原子落盘（先写 .tmp 再 replace）：半截文件比没有文件更糟。"""
    if not sid or not _hist_view_persist_enabled():
        return
    try:
        msgs = [m for m in (view or []) if isinstance(m, dict)]
        msgs = msgs[-HIST_VIEW_STATE_MAX_MSGS:]
        tmp = HIST_VIEW_STATE_PATH + ".tmp"
        os.makedirs(os.path.dirname(HIST_VIEW_STATE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sid": str(sid), "view": msgs}, f, ensure_ascii=False)
        os.replace(tmp, HIST_VIEW_STATE_PATH)
    except Exception:
        pass



SKILLS_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "skills_state.json")
# 存盘上限：只防文件无限长大。正常会话同时激活的技能远少于这个数。
SKILLS_STATE_MAX = 24


def _skills_state_persist_enabled() -> bool:
    """与视图落盘同一开关：自检/探针（tag=test）不读写真实状态文件。"""
    return _trace_tag() != "test"


def _load_skills_state(sid) -> list:
    """读回上一进程激活的技能（按激活先后）。sid 不一致就当没有（防串会话）。

    为什么必须落盘：`_activated_skills` 是纯内存 set，进程一重启就空 →
    工具集从 48 掉回 1 → tools 数组（排在请求最前）整条变样 → 断点回到
    history[0] → 其后整段前缀白付全价。实测 data/turn_metrics.jsonl（12 轮）：
    工具集在 48↔1 之间跳了 5 次，每次跳变都对应一次重启，单轮白烧最高 78k 字符。
    与历史视图同一个病根：把「进程生命周期」当成了「会话边界」。
    任何异常一律降级为「没有状态」——优化代码绝不能弄挂主路径。
    """
    if not sid or not _skills_state_persist_enabled():
        return []
    try:
        with open(SKILLS_STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if str(st.get("sid") or "") != str(sid):
            return []
        names = st.get("skills")
        if not isinstance(names, list):
            return []
        return [str(n) for n in names if isinstance(n, str) and n][:SKILLS_STATE_MAX]
    except Exception:
        return []


def _save_skills_state(sid, order) -> None:
    """原子落盘（先写 .tmp 再 replace）：半截文件比没有文件更糟。"""
    if not sid or not _skills_state_persist_enabled():
        return
    try:
        names = [str(n) for n in (order or []) if isinstance(n, str) and n]
        tmp = SKILLS_STATE_PATH + ".tmp"
        os.makedirs(os.path.dirname(SKILLS_STATE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sid": str(sid), "skills": names[:SKILLS_STATE_MAX]},
                      f, ensure_ascii=False)
        os.replace(tmp, SKILLS_STATE_PATH)
    except Exception:
        pass


def _clear_skills_state() -> None:
    """新会话清盘：盘上的激活集绝不能串到新会话。"""
    if not _skills_state_persist_enabled():
        return
    try:
        os.remove(SKILLS_STATE_PATH)
    except Exception:
        pass



def _fit_tool_result(result: str, tool_name: str) -> str:
    """按工具类型截断过长结果；特殊标记 JSON 原样保留。

    截断必须「自描述」。原来只保留头部 + "..."，模型既不知道被砍了多少、
    也不知道怎么拿到剩下的部分——它极可能以为自己已经看全了，于是凭残缺内容
    下结论（比直接报错更危险）。实测：code_read 请求 500 行只会拿到约 390 行，
    而末尾那句「还有 N 行未显示，可用 x.py:156-195 继续读」恰好被砍掉。
    改成「头部 + 明确的缺口说明 + 尾部（含继续读的提示）」。
    """
    result = str(result or "")
    if _is_special_tool_result(result):
        return result
    limit = _TOOL_RESULT_LIMIT_LONG if tool_name in _LONG_RESULT_TOOLS \
        else _TOOL_RESULT_LIMIT
    if len(result) <= limit:
        return result
    tail_len = min(_TRUNC_TAIL, limit // 5)
    head_len = max(0, limit - tail_len - _TRUNC_NOTICE_RESERVE)
    omitted = max(0, len(result) - head_len - tail_len)
    notice = (
        f"\n\n⋯【结果被截断：原文 {len(result)} 字符，已省略中间 {omitted} 字符】"
        "不要凭这段残缺内容下结论，也不要原样重试。请缩小范围：读文件用 "
        "`路径:起-止` 指定行区间（每次 ≤300 行），搜索加更精确的关键词或 paths 限定，"
        "需要整体结构就先用 symbols / code_locate。\n\n"
    )
    return result[:head_len] + notice + result[-tail_len:]


def _mid_turn_max_tokens() -> int:
    """轮内上下文预算：默认取配置窗口的 1/4（1M 窗口 → 256K），
    也可用 settings.json -> agent.mid_turn_max_tokens 显式覆盖。"""
    try:
        cfg = load_config()
        v = int(cfg.get("agent", {}).get("mid_turn_max_tokens", 0) or 0)
        if v > 0:
            return max(8000, v)
        window = int((cfg.get("usage") or {}).get("context_window", 0) or 0)
        if window > 0:
            return max(80000, window // 4)
    except Exception:
        pass
    return MID_TURN_MAX_TOKENS


def _tool_result_retro_cap() -> int:
    try:
        v = int(load_config().get("agent", {}).get(
            "tool_result_retro_cap", TOOL_RESULT_RETRO_CAP) or TOOL_RESULT_RETRO_CAP)
        return max(50, v)
    except Exception:
        return TOOL_RESULT_RETRO_CAP


def _single_result_max_tokens() -> int:
    """单条工具结果上限：旧轮里超过它的结果，即使总量没超预算也要压。

    第一性原理：轮内一条结果会随其后每次调用被重发。不压时它按「缓存命中价」
    重发，压了则按「未命中价」重发——但体积小得多。设原 S token、压后 C token、
    命中折扣 h，则压划算 ⟺ S·h > C ⟺ 压缩比 > 1/h（h 通常 ≤ 0.25，即 4× 起）。
    retro_cap 默认 600 字符 ≈ 280 token，对 5000 token 的结果压缩比 18×，
    对实测 9810 token 的单条 code_search 结果压缩比 35× —— 都稳赚。
    低于阈值不压：省的命中费抵不过前缀失效的代价，还会让每轮前缀漂移。
    """
    try:
        v = int(load_config().get("agent", {}).get(
            "single_result_max_tokens", SINGLE_RESULT_MAX_TOKENS)
            or SINGLE_RESULT_MAX_TOKENS)
        return max(1000, v)
    except Exception:
        return SINGLE_RESULT_MAX_TOKENS


# ==================== 图片注入：工具结果 → 多模态请求体 ====================
# 工具结果里出现 [[IMG:/abs/path.png]] 时，紧跟着补一条 user 消息把图塞进请求体。
# 为什么不直接把图放进 role=tool：实测提供方会静默丢弃（HTTP 200 但 content 为空串），
# 只有 user 消息 content 数组里的 image_url 才被看见。
_IMG_MARK_RE = re.compile(r"\[\[IMG:([^\]\n]+)\]\]")
_IMG_MAX_SIDE = 1280
_IMG_JPEG_Q = 70
# 本地估算必须用固定值：base64 有 14 万字符，按字符估会把预算撑爆、把工具历史砍光。
# 实测（tools/img_token_probe.py，deepseek-flash @ api.deepseek.com）单图增量随分辨率走：
# 360x640→201、720x1280→592、1280x1280→995，官方「每图封顶 384」在本渠道不成立。
# 原值 400 偏低 33%~60%，上下文预算会被悄悄超支；1024 覆盖 _IMG_MAX_SIDE=1280 的实测上限。
_IMG_TOKEN_EST = 1024


def _img_marks(text) -> list:
    """抽出工具结果里的 [[IMG:path]] 路径（去重、保序）。"""
    try:
        s = str(text or "")
        if "[[IMG:" not in s:
            return []
        seen, out = set(), []
        for p in _IMG_MARK_RE.findall(s):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out
    except Exception:
        return []


def _img_data_url(path: str) -> str:
    """本地图片 → JPEG data URL。实测 1738KB 截图压到 109KB（base64 145KB），省 92%。"""
    try:
        import base64
        import io
        from PIL import Image
        if not os.path.isfile(path):
            return ""
        im = Image.open(path).convert("RGB")
        w, h = im.size
        s = min(1.0, _IMG_MAX_SIDE / max(w, h))
        if s < 1.0:
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=_IMG_JPEG_Q, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.debug("图片编码失败 %s: %s", path, e)
        return ""


def _img_message(path: str, tool_name: str = "") -> Optional[dict]:
    """构造一条把图片喂给模型的多模态 user 消息；编码失败返回 None。"""
    url = _img_data_url(path)
    if not url:
        return None
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"【{tool_name or '工具'} 的图片】{path}"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


_IMG_NO_EYES = ("【图片未注入】当前模型读不到图（供应商/角色卡未标「支持读图」）。"
                "别凭路径猜画面内容，要看图先换支持读图的模型。")

# 判定结果按 (模型, base_url, llm_vision) 缓存 30 秒：模型切走就换 key，
# 用户改配置也能在半分钟内自愈，不必重启进程。
_IMG_VISION_CACHE: dict = {}


def _img_injectable() -> bool:
    """当前模型看不见图就别注入：白传一张图，还可能撞提供方 400。"""
    try:
        import time as _time
        cfg = load_config()
        key = (str(cfg.get("model") or ""), str(cfg.get("base_url") or ""),
               str(cfg.get("llm_vision")))
        now = _time.time()
        hit = _IMG_VISION_CACHE.get(key)
        if hit and now - hit[0] < 30:
            return hit[1]
        from harness.vision_probe import current_can_see
        ok = bool(current_can_see(cfg)[0])
        _IMG_VISION_CACHE[key] = (now, ok)
        return ok
    except Exception:
        return True


def _append_img_messages(messages: list, marks: list, tool_name: str, img_ok: bool) -> int:
    """[[IMG:]] 标记 → 多模态消息；返回追加的字符当量（供预算累计）。

    看不见图时只留 _IMG_NO_EYES 提示、绝不注入 image_url：提示说「未注入」
    就必须真的没注入，否则非视觉模型会撞提供方 400。
    """
    if not marks:
        return 0
    if not img_ok:
        messages.append({"role": "system", "content": _IMG_NO_EYES})
        return 0
    added = 0
    for _ip in marks:
        _im = _img_message(_ip, tool_name)
        if _im:
            messages.append(_im)
            added += _IMG_TOKEN_EST * 4
    return added


# 消息里「不算进 prompt 字符数」的框架字段白名单。
# 反向白名单是刻意的：漏计过两次（reasoning_content、多模态图注），
# 漏计的代价是上下文预算少算一大截且无任何报错。默认计入才让漏项不可能发生。
_MSG_FRAMEWORK_KEYS = frozenset({"role", "tool_call_id", "name", "type"})


def _msg_prompt_chars(msg: dict) -> int:
    """单条消息进 prompt 的字符实长（不含 role/tool_call_id 等框架字段）。"""
    return sum(len(str(v)) for k, v in msg.items()
               if k not in _MSG_FRAMEWORK_KEYS and v is not None)


def _strip_img_for_disk(messages: list) -> list:
    """落盘（断点）前剥掉 base64：一张图 14 万字符，写进断点文件纯浪费。

    只留文字说明；[[IMG:路径]] 标记仍在结果文本里，续跑时按路径重新读。
    """
    out = []
    for m in (messages or []):
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            parts = [str(p.get("text") or "") for p in m["content"]
                     if isinstance(p, dict) and p.get("type") == "text"]
            m = dict(m)
            m["content"] = (" ".join(x for x in parts if x)
                            + "（图片已剥离，需要时按上面的路径重新读）")
        out.append(m)
    return out


def _compact_tool_history(messages: list, budget: int = None,
                          keep_rounds: int = None) -> list:
    """轮内工具历史压缩：把旧工具轮的庞杂结果压成小片段，绑定轮内上下文总量。

    只在总量超预算时动手（平时零开销、逐字节不变，不破坏前缀缓存）；
    压缩只改 assistant/tool/system【工具】消息的 content 长度，
    轮次结构与 tool_calls 配对原样保留，function-calling 校验不受影响。
    """
    if not messages:
        return messages
    try:
        from memory import estimate_tokens
    except Exception:
        return messages
    budget = budget or _mid_turn_max_tokens()
    keep_rounds = KEEP_NEWEST_TOOL_ROUNDS if keep_rounds is None else max(0, int(keep_rounds))
    retro_cap = _tool_result_retro_cap()

    def _est(m):
        # 多模态消息：base64 十几万字符，按字符估会把预算撑爆、把历史砍光
        if isinstance(m.get("content"), list):
            return _IMG_TOKEN_EST
        return estimate_tokens(str(m.get("content") or ""))

    total = sum(_est(m) for m in messages)
    total0 = total          # 原始总量：压缩有没有真跑、省了多少，必须能事后验证
    over_budget = total > budget
    single_cap = _single_result_max_tokens()

    def _is_result_msg(m):
        return (m.get("role") == "tool"
                or (m.get("role") == "system"
                    and str(m.get("content") or "").startswith("【工具")))

    def _shrink(m, cap, suffix):
        """把一条消息压到 cap 字符，并把省下的 token 从 total 里扣掉。

        增量维护是必须的：原来每压完一个旧轮都要把**全部**消息重算一遍 token
        （O(轮数 × 消息数)）。实测 200 条消息 × 40 个旧轮要 600ms，而且同步跑在
        事件循环里——对话会肉眼可见地卡一下，而这一切只是为了算个预算。
        """
        nonlocal total
        c = str(m.get("content") or "")
        if len(c) <= cap:
            return
        new_c = c[:cap] + suffix
        total -= _est(m) - estimate_tokens(new_c)
        m["content"] = new_c

    def _oversize(m):
        """旧轮里的超大单条结果。

        先按字符数粗筛：token 数恒 ≤ 字符数，故 len(c) ≤ cap 必不超阈值，
        只有极少数大消息才需要真算 token——不给每次调用加全量估算开销。
        """
        c = str(m.get("content") or "")
        return len(c) > single_cap and estimate_tokens(c) > single_cap

    # 把消息流切成「assistant + 其后的结果消息」组（原生 tool / 文本【工具】system）
    n = len(messages)
    groups = []          # (start, end, is_tool_round)
    i = 0
    while i < n:
        if messages[i].get("role") == "assistant":
            j = i + 1
            while j < n and _is_result_msg(messages[j]):
                j += 1
            groups.append((i, j, j > i + 1))
            i = j
        else:
            i += 1
    tool_groups = [g for g in groups if g[2]]
    if not tool_groups:
        return messages

    # 1) 压缩旧轮（保留最新 keep_rounds 轮完整）。
    #    缓存友好：从「最新旧轮」往「最旧轮」逐个截断，一旦总量回到预算内就停——
    #    只改最少的内容，且失效点尽量靠近动态尾巴（改动越靠后，前缀缓存失效范围越小）。
    old_groups = list(tool_groups[:-keep_rounds] if keep_rounds else [])
    for start, end, _ in reversed(old_groups):
        for k in range(start, end):
            m = messages[k]
            if _is_result_msg(m):
                # 两条触发线：总量超预算（按序截断到预算内），或旧轮里存在
                # 超大单条结果（压缩比 > 1/h 即划算，见 _single_result_max_tokens）
                if over_budget or _oversize(m):
                    _shrink(m, retro_cap, "…【已压缩】")
            elif m.get("role") == "assistant" and over_budget:
                _shrink(m, ASSISTANT_RETRO_CAP, "…")
        if over_budget and total <= budget:
            break

    # 未超预算、且一条都没压：逐字节原样返回，绝不制造无谓的前缀失效
    if not over_budget and total == total0:
        return messages

    # 2) 仍超预算：最新一轮的结果先压到 2000 字（仍够模型继续干活），
    #    再超才压到 600 字，兜底保证请求一定能发出去
    if total > budget and tool_groups:
        for cap in (2000, 600):
            start, end, _ = tool_groups[-1]
            for k in range(start, end):
                m = messages[k]
                if _is_result_msg(m):
                    _shrink(m, cap, "…【已压缩】")
            if total <= budget:
                break
    if total < total0:
        logger.info(
            "轮内工具历史压缩：预算 %d token，%d → %d（省 %d，%.0f%%）%s",
            budget, total0, total, total0 - total,
            100.0 * (total0 - total) / max(1, total0),
            "" if over_budget else "｜触发：旧轮超大单条 > %d token" % single_cap)
    return messages


def _normalize_tool_rounds(messages: list) -> list:
    """兜底规范化：保证所有 role=tool 消息都带 tool_call_id，且与最近的
    assistant tool_calls 一一对齐。

    背景：工具结果入库（memory.add_message("tool", result)）历史上只存了
    content、没存 tool_call_id。下次从记忆库（DB）恢复历史时，role=tool 的
    消息就缺 tool_call_id，发给 OpenAI 兼容提供方会 400
    （missing field tool_call_id）。这里按「assistant(tool_calls) 之后的
    连续 tool 消息」分组，从 assistant 的 tool_calls ids 里按序补回；
    若 tool 消息多于 ids（数据异常），补一个占位 id，保证字段一定存在，
    不再因缺字段被拒。

    v2 增强：除「assistant 紧邻之后」外，还兜住两条漏网路径——
    1) 孤立 tool 消息（其前置 assistant(tool_calls) 已被 _compact_tool_history
       压缩丢弃，或本就没前置）：现在会向后扫描最近一个带 tool_calls 的
       assistant 的 ids 补回；仍无则用占位 id（call_tool_{index}），绝不缺字段。
    2) tool 消息的 tool_call_id 未落到任何已知 assistant ids 上（错位/陈旧）：
       直接重写为最近一个 assistant tool_calls 里尚未被本段引用的 id，
       保证与上游 tool_calls 严格一一对应，功能调用校验不会 400。
    """
    if not messages:
        return messages
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            j = i + 1
            # 收集 assistant(tool_calls) 之后的连续 tool 消息
            tool_msgs = []
            while j < n and messages[j].get("role") == "tool":
                tool_msgs.append(messages[j])
                j += 1
            k = 0
            for tm in tool_msgs:
                if not tm.get("tool_call_id"):
                    tm["tool_call_id"] = (
                        ids[k] if k < len(ids) else f"call_{j}"
                    )
                k += 1
            # v3：tool 消息少于 tool_calls → 补齐缺失的响应，避免 provider 400
            if len(tool_msgs) < len(ids):
                used = {tm.get("tool_call_id") for tm in tool_msgs}
                missing = [tid for tid in ids if tid not in used]
                for tid in missing:
                    messages.insert(j, {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "【工具结果缺失】",
                    })
                    j += 1
                    n += 1
            i = j
        else:
            i += 1

    # v2：兜住孤立/错位的 tool 消息（前述路径补不到的）。
    # 先收集所有 assistant tool_calls 的 id 全集，供错位重写复用。
    known_ids = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            known_ids.extend(tc.get("id") for tc in m["tool_calls"] if tc.get("id"))
    seen_tool_ids = [m.get("tool_call_id") for m in messages
                     if m.get("role") == "tool"]
    remaining_ids = [tid for tid in known_ids if tid not in seen_tool_ids]

    for idx, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        cur = m.get("tool_call_id")
        if not cur:
            # 孤立 tool：从「最近一个带 ids 的 assistant」按序补；无则占位
            if remaining_ids:
                m["tool_call_id"] = remaining_ids.pop(0)
            else:
                m["tool_call_id"] = f"call_tool_{idx}"
        elif cur not in known_ids:
            # 错位/陈旧的 id（不在任何 assistant tool_calls 里）：重写为可用 id
            if remaining_ids:
                m["tool_call_id"] = remaining_ids.pop(0)
            else:
                m["tool_call_id"] = f"call_tool_{idx}"
    # v3 结构修复：保证每条 role=tool 消息前面紧邻的是「带 tool_calls 的 assistant」。
    # 背景：记忆库/断点恢复路径里，tool 结果可能没关联到 assistant(tool_calls)，孤立地
    # 出现在 user 或纯 content 的 assistant 之后。此时即便 tool_call_id 已补齐，OpenAI
    # 兼容提供方仍会因「tool 消息前面不是带 tool_calls 的 assistant」而拒绝
    # （400: role 'tool' must be a response to a preceding message with 'tool_calls'）。
    # 这里为这样的孤立/错位 tool 在紧贴其前插入一条合成 assistant(tool_calls)，挂上该 id，
    # 使结构合法。扫描时跳过同组的连续 tool（assistant(tc=[c0,c1]) 后跟 tool(c0)/tool(c1)
    # 属合法结构，不得误判）。
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.get("role") != "tool":
            i += 1
            continue
        # 从 i 向前扫到最近一条「非 tool」消息作为潜在 host
        j = i - 1
        while j >= 0 and messages[j].get("role") == "tool":
            j -= 1
        host = messages[j] if j >= 0 else None
        tid = m.get("tool_call_id")
        host_ok = bool(
            host and host.get("role") == "assistant" and host.get("tool_calls")
            and any(tc.get("id") == tid for tc in host["tool_calls"])
        )
        if not host_ok:
            new_tid = tid or f"call_tool_{i}"
            m["tool_call_id"] = new_tid
            messages.insert(i, {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": new_tid,
                    "type": "function",
                    "function": {"name": "_recovered_tool", "arguments": "{}"},
                }],
            })
            i += 1
            n += 1
        i += 1
    return messages


def _ensure_reasoning_echo(messages: list) -> list:
    """给缺 reasoning_content 的 assistant 消息补空串（thinking 渠道的硬要求）。

    为什么必须补在唯一出口：全仓只有 4785/4811 两处在 round_reasoning 非空时挂这个
    字段，而历史注入(3837/3843)、孤立 tool 修复(825 合成 assistant)、断点续跑与记忆
    恢复产出的 assistant 天生没有它——任一混进请求就被渠道整轮 400
    （"The reasoning_content in the thinking mode must be passed back to the API"）。

    补空串而非删字段：空串不进 prompt token（前缀不变，命中价不受影响），
    字段存在即可过校验；实测该渠道接受空串与占位文本。
    """
    out = []
    for m in messages:
        if (isinstance(m, dict) and m.get("role") == "assistant"
                and "reasoning_content" not in m):
            m = dict(m)
            m["reasoning_content"] = ""
        out.append(m)
    return out


def _tool_max_tokens() -> int:
    """工具调用轮的 LLM 输出上限：写大文件（skill.py 等）需要大输出预算。

    settings.json 顶层可配 tool_max_tokens（默认 4096）；未配置时至少不低于
    普通 max_tokens，避免模型生成长工具参数（如整体写 skill.py）写到一半被截断。
    """
    try:
        cfg = load_config()
        v = int(cfg.get("tool_max_tokens") or 0)
        if v > 0:
            return max(512, min(v, 16384))
        base = int(cfg.get("max_tokens") or 512)
    except Exception:
        base = 512
    return max(base, 4096)


def _stream_retry_count() -> int:
    """流式输出中途断网的最大重建次数（settings.json -> agent.stream_retries）。

    默认 9999 = 近似无限：断网/服务波动时持续自动重建流，只有用户输入能打断，
    避免网络抖动导致整轮回复被中止。
    """
    try:
        v = int(load_config().get("agent", {}).get("stream_retries", 9999) or 0)
        return max(0, min(v, 9999))
    except Exception:
        return 9999


def _llm_retry_window_sec() -> float:
    """LLM 瞬时错误自动重连的最长坚持时长（settings.json ->
    agent.llm_retry_window_sec，默认 1800s）。窗口内无限重试等网络恢复，
    超过窗口仍失败才放弃（防服务端真挂了时任务永久卡死）。"""
    try:
        v = float(load_config().get("agent", {}).get("llm_retry_window_sec", 1800) or 0)
        return max(60.0, v)
    except Exception:
        return 1800.0


def _is_retryable_llm_error(e: BaseException) -> bool:
    """LLM 调用异常是否值得自动重连：瞬时网络错误 + 渠道熔断冷却。

    熔断（SupervisedBlockedError）是 harness 在连续失败后的快速失败保护，
    冷却结束后会半开探测自动恢复——等待即可，不应因此中止对话轮。
    """
    try:
        from harness.core import is_transient_error
        if is_transient_error(e):
            return True
    except Exception:
        pass
    if type(e).__name__ == "SupervisedBlockedError":
        return True
    msg = (str(e) or "").lower()
    return any(k in msg for k in ("熔断", "rate limit", "429", "timeout", "connection"))


def _is_transient_stream_error(e: Exception) -> bool:
    """判断流式中途断网是否值得重建（瞬时错误才重试）。"""
    try:
        from harness.core import is_transient_error
        return is_transient_error(e)
    except Exception:
        msg = (str(e) or "").lower()
        return any(k in msg for k in (
            "timeout", "timed out", "connection", "refused", "reset",
            "closed", "rate limit", "temporarily", "429", "500", "502",
            "503", "504",
        ))


def _dedup_stream_text(assistant_content: str, chunk: str,
                       delivered_text: str, skip_prefix_len: int):
    """流式重试时的文本前缀去重。

    返回 (assistant_content, delivered_text, skip_prefix_len, yield_text)：
    - yield_text 为 None 表示本块仍落在已播报前缀内（只累积、不输出）；
    - 重新生成内容与已播报不一致时从头输出（宁可轻微重复也不丢内容）。
    """
    if skip_prefix_len <= 0:
        return (assistant_content + chunk, delivered_text + chunk, 0, chunk)
    merged = assistant_content + chunk
    overlap = delivered_text[:skip_prefix_len]
    if merged[:skip_prefix_len] == overlap:
        if len(merged) <= skip_prefix_len:
            return merged, delivered_text, skip_prefix_len, None
        tail = merged[skip_prefix_len:]
        return merged, delivered_text + tail, 0, tail
    return merged, delivered_text + merged, 0, merged


def _extract_balanced_json(text: str) -> Optional[str]:
    """提取文本中第一个完整的 JSON 对象（对字符串值里的 { } 免疫，避免提前截断）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def load_config():
    with open(BASE_DIR / "settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- 用户级角色卡片（切卡只影响自己） ----------
# 卡片本身是数据源；role_card_users.json 只存「谁当前用哪张卡」的指针。
# 全局 settings.json / tts_config.json 退居兜底：没有卡片归属的调用方
# （长跑引擎、定时任务、子智能体等无头入口）仍读它，行为不变。
ROLE_CARD_USERS_FILE = BASE_DIR / "role_card_users.json"
CHARACTER_CARDS_FILE = BASE_DIR / "character_cards.json"


def _read_json_or(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_card_users() -> dict:
    """uid -> card_id：每个用户当前生效的角色卡片。"""
    data = _read_json_or(ROLE_CARD_USERS_FILE, {})
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def save_card_users(users: dict) -> None:
    tmp = str(ROLE_CARD_USERS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROLE_CARD_USERS_FILE)


def load_character_cards() -> list:
    data = _read_json_or(CHARACTER_CARDS_FILE, {})
    cards = data.get("cards") if isinstance(data, dict) else data
    return cards if isinstance(cards, list) else []


def active_role_card_id(user_id: str) -> str:
    return (load_card_users().get(user_id or "") or "").strip()


def role_card_of(user_id: str) -> Optional[dict]:
    """该用户当前生效的卡片；没有（从未切过卡 / 卡片已被删）返回 None。"""
    card_id = active_role_card_id(user_id)
    if not card_id:
        return None
    return next((c for c in load_character_cards() if c.get("id") == card_id), None)


def role_card_persona(user_id: str) -> dict:
    """用户级人设：卡片优先，无卡片回落全局 settings.json（无头调用走这条）。"""
    cfg = load_config()
    card = role_card_of(user_id)
    if not card:
        return {
            "role_name": (cfg.get("role_name") or "").strip(),
            "system_prompt": cfg.get("system_prompt", ""),
            "user_name": (cfg.get("user_name") or "").strip(),
            "tools": cfg.get("tools"),
        }
    return {
        "role_name": (card.get("role_name") or card.get("name") or "").strip(),
        "system_prompt": card.get("system_prompt", ""),
        "user_name": (card.get("user_name") or "").strip(),
        "tools": card.get("tools"),
    }


def load_config_for(user_id: str) -> dict:
    """全局配置 + 该用户卡片的 LLM 声明覆盖。

    卡片只声明 provider_id / model / temperature / vision；供应商的
    base_url / api_key 取自全局注册表，绝不在卡片里各存一套。
    """
    cfg = load_config()
    card = role_card_of(user_id)
    if not card:
        return cfg
    llm = card.get("llm") or {}
    providers = cfg.get("llm_providers")
    pid = (llm.get("provider_id") or "").strip()
    prov = None
    if pid and isinstance(providers, list):
        prov = next((p for p in providers
                     if isinstance(p, dict) and str(p.get("id") or "") == pid), None)
    if prov:
        base = str(prov.get("base_url") or "").strip()
        if base:
            cfg["base_url"] = base
            cfg["api_key"] = str(prov.get("api_key") or "")
        cfg["llm_provider_id"] = pid
    if (llm.get("model") or "").strip():
        cfg["model"] = llm["model"].strip()
    elif prov and (prov.get("default_model") or "").strip():
        cfg["model"] = prov["default_model"].strip()
    if llm.get("temperature") is not None:
        cfg["temperature"] = llm["temperature"]
    if "vision" in llm:
        cfg["llm_vision"] = bool(llm["vision"])
    tools = card.get("tools")
    if isinstance(tools, dict):
        cfg.setdefault("agent", {})
        cfg["agent"]["enable_tools"] = bool(tools.get("enabled", True))
        cfg["agent"]["allowed_tools"] = list(tools.get("allowed", []) or [])
    return cfg


def migrate_legacy_active_card(user_ids: Optional[list] = None) -> bool:
    """一次性迁移：settings.json 的 active_role_card 曾经是全机唯一一份。

    按人隔离后它不再被读写——不迁移的话，主人的记忆空间会从 role_card:<id>
    掉回 default，旧对话凭空消失。把这份卡片绑给部署者本人（统一身份 + 管理员 uid）。
    """
    try:
        owner = ""
        try:
            owner = str((load_config().get("agent") or {}).get("unified_user_id") or "").strip()
        except Exception:
            owner = ""
        owner = owner or "default"
        users = load_card_users()
        if users.get(owner):
            return False
        legacy = ""
        try:
            legacy = str(load_config().get("active_role_card") or "").strip()
        except Exception:
            legacy = ""
        if not legacy or not any(c.get("id") == legacy for c in load_character_cards()):
            return False
        for uid in [owner] + [str(u) for u in (user_ids or []) if u]:
            users[uid] = legacy
        save_card_users(users)
        logger.info(f"角色卡片迁移：{legacy} 已绑给 {len(users)} 个部署者身份")
        return True
    except Exception as e:
        logger.warning(f"角色卡片迁移失败（忽略）: {e}")
        return False


def _set_agent_mode(mode: str) -> None:
    """把模式偏好原子写回 settings.json（失败静默，绝不影响对话）。"""
    try:
        path = BASE_DIR / "settings.json"
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("agent", {})["mode"] = mode
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _strip_think_markers(text: str) -> str:
    """剔除模型输出里可能残留的 <think>/</think> 等思维标记（污染正文/思维链显示）。"""
    if not text:
        return text
    return re.sub(r"</?think[^>]*>", "", text, flags=re.IGNORECASE)


# ---- 模式：同一身份、两种姿态（日常 / 工程），像人一样切换工作状态 ----
_MODE_SWITCH_TO_PROG = ("切到工程模式", "工程模式", "编程模式", "工作模式", "进入工程")
_MODE_SWITCH_TO_DAILY = ("日常模式", "切回日常", "回到日常", "退出工程", "聊天模式")
_COMPLEX_HINTS = (
    "优化", "重构", "项目", "代码", "脚本", "文件", "修复", "改一下", "改个",
    "写一个", "部署", "实现", "查一下", "搜索", "找一下", "下载", "测试",
    "编译", "错误", "报错", "配置", "接口", "任务", "调试",
)


def _looks_complex(message: str) -> bool:
    """auto 模式下的简单启发式：较长且带任务词 → 本轮按工程姿态处理。"""
    m = (message or "").strip()
    if len(m) < 8:
        return False
    return any(h in m for h in _COMPLEX_HINTS)


def _max_tool_rounds() -> int:
    """读取工具调用最大轮数（settings.json -> agent.max_tool_rounds）。

    0 / 负数 / 未配置 表示不限制：循环一直持续到模型不再发起工具调用为止。
    """
    try:
        v = int(load_config().get("agent", {}).get("max_tool_rounds", MAX_TOOL_ROUNDS) or 0)
    except Exception:
        v = MAX_TOOL_ROUNDS
    return max(v, 0)


def _repeat_guard_limit() -> int:
    """读取死循环防护阈值（settings.json -> agent.repeat_guard_rounds，默认 10）。"""
    try:
        v = int(load_config().get("agent", {}).get(
            "repeat_guard_rounds", REPEAT_GUARD_LIMIT) or REPEAT_GUARD_LIMIT)
    except Exception:
        v = REPEAT_GUARD_LIMIT
    return max(v, 2)


def _work_lean_context() -> bool:
    """工程模式下是否启用「工作状态瘦身」。

    开启后（默认 true），大白进入工程模式时会动态卸载与任务无关的上下文
    （娱乐/外观/场景/音乐/视频/动作等），只保留对完成项目有用的部分
    （工具规则、任务经验、子智能体状态、相关记忆），让有限的上下文预算
    全部花在当前任务上。settings.json -> agent.work_lean_context 可关。
    """
    try:
        return bool(load_config().get("agent", {}).get("work_lean_context", True))
    except Exception:
        return True


def _reasoning_extra(mode: str = "daily") -> Optional[dict]:
    """按模式返回推理强度参数（extra_body 形式），只对硅基流动推理模型生效。

    - daily（日常闲聊）：低强度 + 小预算，简单问题快速回应、不空转；
    - programming（工程任务）：高强度 + 大预算，复杂任务深入思考；
    - 预算 <= 0 / 未配置 = 不设上限：不下发 thinking_budget，让模型想多久想多久；
    - 强度与预算可在 settings.json -> reasoning 里覆盖，enabled=false 可整体关闭。
    其他渠道（Ollama / opencode zen 等）不认识这些参数，一律返回 None。
    """
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    base = str(cfg.get("base_url") or "")
    model = str(cfg.get("model") or "")
    if "siliconflow.cn" not in base:
        return None
    if not (model.startswith("deepseek-ai/") or model.startswith("Qwen/")):
        return None
    r = cfg.get("reasoning") or {}
    if r.get("enabled") is False:
        return None
    if mode == "programming":
        effort = r.get("programming_effort") or "high"
        budget = r.get("programming_thinking_budget", 0)
    else:
        effort = r.get("daily_effort") or "low"
        budget = r.get("daily_thinking_budget", 0)
    extra = {}
    try:
        effort = str(effort).strip().lower()
        if effort in ("low", "medium", "high", "max"):
            extra["reasoning_effort"] = effort
    except Exception:
        pass
    try:
        budget = int(budget)
        if budget > 0:  # <=0 / 未配置 = 不限制思考长度
            extra["thinking_budget"] = budget
    except (TypeError, ValueError):
        pass
    # Qwen3 推理模型（8B/14B/32B、*-Thinking 等）必须显式 enable_thinking，
    # 思考才会放进 reasoning_content；不带该参数时模型会把思路写进正文。
    # 只对确认支持推理的型号下发，避免 Instruct / 3.5 / 3.6 系列误传参数
    try:
        if re.match(r"^Qwen/Qwen3-(?:\d{1,2}B|[\w.-]*Thinking)(?:/|$)",
                    str(model)):
            extra["enable_thinking"] = True
    except Exception:
        pass
    return extra or None


def _needs_reasoning_echo(model) -> bool:
    """thinking 模式渠道（DeepSeek 系）要求多轮请求把 assistant 的 reasoning_content
    原样回传，丢了就整轮 400（"must be passed back to the API"）。
    只对确认这类模型回传，避免其他渠道拒收未知字段。"""
    try:
        return bool(re.search(r"deepseek|reasoner|thinking", str(model), re.I))
    except Exception:
        return False


# ---- 规则区三条硬规则（删除/画图/音乐）的埋点分类 ----
# 这三条此前无任何埋点，删留只能靠感觉。这里只回答「该行为发生过没有」，
# 不判断行为对错——对错由 tools/prompt_rules_audit.py 结合上下文裁。
# 删除判定保守但不能假阴：假阴会得出「这条规则从没被触发过」的反向结论。
_DELETE_CMD_RE = re.compile(
    r"(?:^|[;&|]\s*|\bsudo\s+|\bxargs\s+)(?:rm|rmdir|unlink|shred)\b"
    r"|\bgit\s+rm\b|\s-delete\b|\btruncate\b\s+-s\s*0"
)
_SHELL_TOOLS = {"shell_run", "run_shell", "shell", "bash", "sh"}


def _rule_op_kinds(tool_name: str, arguments) -> tuple:
    """把一次工具调用归类到规则区埋点：返回 (画图, 音乐, 删除) 三个 0/1。

    删除这一类故意放宽到「名字里带 delete/remove」+ shell 删除命令：
    workspaces_remove 这类非文件删除也算进来，它同样是「删除请求」。
    计数只用于「规则有没有作用面」，不用于精确统计删了几个文件。
    """
    n = str(tool_name or "").lower()
    img = 1 if n.startswith("image_gen") else 0
    music = 1 if n.startswith("music_") else 0
    delete = 1 if ("delete" in n or "remove" in n) else 0
    if not delete and n in _SHELL_TOOLS:
        cmd = ""
        if isinstance(arguments, dict):
            cmd = str(arguments.get("command") or arguments.get("cmd") or "")
        if cmd:
            # 先摘掉丢弃式重定向：`ls 2>/dev/null` 是纯读，不是删除也不是写
            bare = re.sub(r"\d?>\s*&\d|\d?>\s*/dev/null", " ", cmd)
            if _DELETE_CMD_RE.search(bare):
                delete = 1
    return img, music, delete


def _tool_fp(name: str, arguments: dict) -> str:
    """工具调用指纹：工具名 + 参数 JSON（键排序），用于死循环检测。"""
    try:
        return f"{name}|{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
    except Exception:
        return f"{name}|{str(arguments)}"


def _result_fp(result) -> str:
    """工具结果指纹：只判断「结果有没有变化」，不保留原文。

    用 blake2b 而非内置 hash：内置 hash 对 str 逐进程随机化，虽然同一进程内
    一致（本用途够用），但指纹会出现在日志和测试断言里，稳定的摘要更好排查。

    全量 hash，不做头尾截断：几万字符一次摘要只有几十微秒，相对一次 LLM
    往返（秒级）完全可以忽略；而截断会漏掉「中间变了」的情况——
    死循环判定宁可多算，不可漏判。
    """
    try:
        s = result if isinstance(result, str) else str(result)
        return hashlib.blake2b(s.encode("utf-8", "replace"), digest_size=8).hexdigest()
    except Exception:
        return "?"


def _same_result_guard_limit() -> int:
    """原地打转阈值（settings.json -> agent.same_result_guard_rounds，默认 4）。"""
    try:
        v = int(load_config().get("agent", {}).get(
            "same_result_guard_rounds", SAME_RESULT_GUARD_LIMIT) or SAME_RESULT_GUARD_LIMIT)
    except Exception:
        v = SAME_RESULT_GUARD_LIMIT
    return max(v, 2)


# 重复调用提示：模型用完全相同的工具+参数再调一次时，结果必然与上次一致。
# 光靠缓存返回同样的内容，它不会「意识到」自己在原地打转——附一句提示，循环
# 才有可能自己终止。每避免一次重复调用，省下的是整整一个 LLM 往返（秒级，
# 是整轮里最贵的部分）。只加给 LLM 看的那份，不污染记忆库与前端展示。
REPEAT_CALL_HINT = (
    "\n\n（提示：本轮此前已用完全相同的参数调用过该工具，结果与上次一致、不会变化。"
    "请直接使用已有结果继续，或换参数/换工具推进，不要重复同样的调用。）"
)


# 跨轮工具调用指纹的上限。长会话里指纹会持续累积，但它只服务统计，
# 淘汰最旧的只会让数字略偏保守，不会出错。
_CROSS_FP_LIMIT = 4000


# 「连续单发只读」检测：只统计同一轮 pending 里的同名工具（见 _tool_name_counts）
# 看不见真正烧时间的那类浪费 —— 模型每轮只发 1 个只读调用、连发好几轮，每轮付一次
# 秒级 LLM 往返。判据必须严格（本轮恰好 1 个工具且它只读），否则提示会打在正常行为上。
SINGLE_RO_STREAK_N = 3
SINGLE_RO_NAMES_MAX = 6


def _is_readonly_tool(name: str) -> bool:
    """只读判定，唯一权威名单是 harness.tool_sched.READONLY_TOOLS。
    拿不到调度器就返回 False —— 宁可漏报，也不能在写工具上暗示「可以并行」。"""
    try:
        from harness.tool_sched import is_readonly
        return is_readonly(name)
    except Exception:
        return False


def _single_ro_note(streak: int, names: list, pending_names: list) -> tuple:
    """更新「连续单发只读」计数，返回 (streak, names, 本轮是否该给反馈)。

    只有「本轮恰好 1 个工具且它只读」才累加；多工具、写工具、纯文本轮一律清零。
    每满 SINGLE_RO_STREAK_N 轮给一次反馈而非每轮都给：长串行会被覆盖，又不会刷屏。
    """
    if len(pending_names) == 1 and _is_readonly_tool(pending_names[0]):
        streak += 1
        names = (list(names) + [pending_names[0]])[-SINGLE_RO_NAMES_MAX:]
        return streak, names, streak % SINGLE_RO_STREAK_N == 0
    return 0, [], False


def _get_tool_cache():
    """只读工具结果缓存（harness.tool_sched）。harness 不可用时返回 None——
    缓存只是优化，拿不到就照常执行，绝不能成为故障源。"""
    try:
        from harness.tool_sched import get_cache
        return get_cache()
    except Exception:
        return None


def _plan_batches(pending):
    """把本轮待执行工具切成「批内可安全并行、批间必须串行」的批次。

    为什么不能只用一个「有冲突就全串行」的开关：一轮里只要有 1 个影响面未知的
    工具（如 shell_run 跑测试/构建），那些本可并行的只读读取就会被一起拖成串行。
    大型任务里这种混合轮很常见，白白多等数倍时间。

    只读永远在第一批并行跑，写工具按资源键分桶、同键必分属不同批 —— 正确性不变。
    调度器不可用/出错 → 返回 None，调用方退回完全串行（宁可慢，不可错）。
    """
    try:
        from harness.tool_sched import plan
        return plan(pending) or None
    except Exception as e:
        logger.warning(f"工具分批调度失败，本轮退回串行: {e}")
        return None


def tool_exec_config(tool_name: str = "") -> dict:
    """工具执行的超时与心跳配置（settings.json -> agent 段），失败用默认值。

    长耗时工具（Blender 转换/Mixamo 下载/工作树命令等）按 _TOOL_TIMEOUT_OVERRIDES
    或 settings.json -> agent.tool_timeouts 放大超时，避免"工具还在正常干活就被误杀"；
    其余工具保持默认，绝不无限等待。

    模块级而非 Agent 方法：子智能体也要用同一份超时口径。两份配置各自维护，
    迟早漂移成「主智能体 30s、子智能体不限时」这种不一致——子智能体卡死时
    没有任何超时能救它（只有 2 小时的看门狗兜底），是实打实的效率黑洞。
    """
    try:
        cfg = load_config().get("agent", {}) or {}
        timeout = float(cfg.get("tool_call_timeout", TOOL_CALL_TIMEOUT) or TOOL_CALL_TIMEOUT)
        heartbeat = float(cfg.get("tool_heartbeat_interval_sec", TOOL_HEARTBEAT_INTERVAL)
                          or TOOL_HEARTBEAT_INTERVAL)
        overrides = dict(cfg.get("tool_timeouts") or {})
        for k, v in _TOOL_TIMEOUT_OVERRIDES.items():
            overrides.setdefault(k, v)
        if tool_name:
            try:
                timeout = float(overrides.get(tool_name, timeout) or timeout)
            except (TypeError, ValueError):
                pass
    except Exception:
        timeout, heartbeat = TOOL_CALL_TIMEOUT, TOOL_HEARTBEAT_INTERVAL
    # 长任务（如 shell 长命令、文件搜索）允许更久，但绝不无限等待
    timeout = max(10.0, min(float(timeout), TOOL_CALL_TIMEOUT_MAX))
    heartbeat = max(1.0, min(float(heartbeat), 60.0))
    return {"timeout": timeout, "heartbeat": heartbeat}


# 热重载联动：当前是否有对话轮（含工具调用）正在执行；有则延迟自动重启，
# 避免大白自己通过工具调用修改核心代码时，中途被热重载重启打断（直到本轮说完）。
_turn_active_count = 0
_turn_count_lock = threading.Lock()


def _turn_begin() -> None:
    global _turn_active_count
    with _turn_count_lock:
        _turn_active_count += 1


def _turn_end() -> None:
    global _turn_active_count
    with _turn_count_lock:
        if _turn_active_count > 0:
            _turn_active_count -= 1


def active_turns() -> int:
    """当前正在执行的对话轮数（0 = 空闲，可以安全重启）。"""
    return _turn_active_count


# ==================== 对话轮断点（热重载中途自主恢复） ====================
# 角色调用工具期间允许热重载：每轮工具执行前把「完整 LLM 消息 + 待执行工具」
# 原子落盘，进程被热重载杀死后，server 启动/客户端重连时读取断点，角色自主
# 续跑被打断的那一轮——验证和迭代不再被「延迟 30 分钟重启」卡住。
TURN_CKPT_DIR = BASE_DIR / "data" / "turn_checkpoints"
_TURN_CKPT_LOCK = threading.Lock()
# 2026-08-29 多槽位化：断点按 turn_id 独立落盘，每用户最多保留 8 个。
# 修复"用户插话打断 → 新对话轮把断点清掉 → 说『继续』只剩一句话摘要"的问题：
# 新轮不再覆盖/删除旧轮断点，被打断的任务（paused 状态）一直保留到被『继续』
# 真正续跑完成，或被用户显式作废（clear_turn_checkpoint）。
_TURN_CKPT_MAX_SLOTS = 8


def _turn_ckpt_enabled() -> bool:
    """断点开关：settings.json -> agent.turn_checkpoint（默认开启）。"""
    try:
        cfg = load_config().get("agent") or {}
        return bool(cfg.get("turn_checkpoint", True))
    except Exception:
        return True


def _user_ckpt_safe(user_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(user_id or "default")) or "default"
    return safe


def _turn_ckpt_slot_path(user_id: str, turn_id: str) -> Path:
    safe = _user_ckpt_safe(user_id)
    tid = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(turn_id or "")) or "unknown"
    return TURN_CKPT_DIR / f"{safe}__{tid}.json"


def _turn_ckpt_latest_path(user_id: str) -> Path:
    return TURN_CKPT_DIR / f"{_user_ckpt_safe(user_id)}.latest"


def _list_ckpt_slot_paths(user_id: str = None) -> list:
    """列出断点槽位文件（新→旧按 mtime；兼容旧版单文件 <user>.json）。"""
    if not TURN_CKPT_DIR.is_dir():
        return []
    if user_id is None:
        paths = list(TURN_CKPT_DIR.glob("*__*.json"))
        paths += [p for p in TURN_CKPT_DIR.glob("*.json")
                  if "__" not in p.name]
    else:
        safe = _user_ckpt_safe(user_id)
        paths = list(TURN_CKPT_DIR.glob(f"{safe}__*.json"))
        legacy = TURN_CKPT_DIR / f"{safe}.json"
        if legacy.exists():
            paths.append(legacy)
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_ckpt_slot(path: Path) -> Optional[dict]:
    try:
        with _TURN_CKPT_LOCK:
            cp = json.loads(path.read_text(encoding="utf-8"))
        return cp if isinstance(cp, dict) else None
    except Exception:
        return None


def _evict_old_ckpt_slots(user_id: str) -> None:
    """每用户槽位封顶：超出按时间淘汰最旧的（保留最近任务优先）。"""
    try:
        slots = _list_ckpt_slot_paths(user_id)
        if len(slots) <= _TURN_CKPT_MAX_SLOTS:
            return
        with _TURN_CKPT_LOCK:
            for p in slots[_TURN_CKPT_MAX_SLOTS:]:
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def save_turn_checkpoint(user_id: str, cp: dict) -> None:
    """原子写断点槽位 + 更新该用户 latest 指针。

    每轮独立槽位（文件名含 turn_id），进程被杀时磁盘上始终有完整的旧断点；
    latest 指针指向最近一次保存的轮次，供「继续」/状态读取快速定位。
    """
    try:
        with _TURN_CKPT_LOCK:
            TURN_CKPT_DIR.mkdir(parents=True, exist_ok=True)
            path = _turn_ckpt_slot_path(user_id, cp.get("turn_id") or "")
            tmp = path.with_suffix(".json.tmp")
            # 紧凑序列化：断点是给程序读的，indent=2 只是让人眼舒服，
            # 代价是体积 +8~14%、序列化时间 +15%。而它每轮要写两次、写整个
            # 轮内消息列表——长任务里这是纯浪费的磁盘 IO 与 CPU。
            tmp.write_text(json.dumps(cp, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, path)
            latest = _turn_ckpt_latest_path(user_id)
            latest.write_text(str(cp.get("turn_id") or ""), encoding="utf-8")
        _evict_old_ckpt_slots(user_id)
    except Exception as e:
        logger.warning(f"保存对话轮断点失败: {e}")


def load_turn_checkpoint(user_id: str, only_paused: bool = False) -> Optional[dict]:
    """读取该用户最新断点（latest 指针优先；指针失效时回退扫描槽位）。

    only_paused=True 时只返回最新一条「已暂停」断点——用户说『继续』时的
    续跑源（普通新对话轮不会覆盖它，任务可一直等到被续跑）。
    """
    try:
        latest = _turn_ckpt_latest_path(user_id)
        if latest.exists():
            tid = latest.read_text(encoding="utf-8").strip()
            if tid:
                cp = _read_ckpt_slot(_turn_ckpt_slot_path(user_id, tid))
                if cp and (not only_paused or cp.get("paused")):
                    return cp
        for p in _list_ckpt_slot_paths(user_id):
            cp = _read_ckpt_slot(p)
            if cp and (not only_paused or cp.get("paused")):
                return cp
    except Exception as e:
        logger.warning(f"读取对话轮断点失败: {e}")
    return None


def load_paused_turn_checkpoint(user_id: str) -> Optional[dict]:
    """最新一条「已暂停」断点（供『继续』指令续跑）。"""
    return load_turn_checkpoint(user_id, only_paused=True)


# 「继续」自动续跑的新鲜窗口（秒）：超过该时间视为任务已过期，
# 不再自动恢复旧断点/旧摘要，避免把过时任务总结注入当前对话。
# settings.json -> agent.resume_fresh_seconds 可调。
RESUME_FRESH_SECONDS = 1800


def resume_fresh_seconds() -> int:
    try:
        v = int(load_config().get("agent", {}).get(
            "resume_fresh_seconds", RESUME_FRESH_SECONDS) or RESUME_FRESH_SECONDS)
        return max(60, v)
    except Exception:
        return RESUME_FRESH_SECONDS


def ckpt_is_fresh(cp: dict, now: float = None) -> bool:
    """断点是否仍在「继续」新鲜窗口内（updated_at 距今 ≤ resume_fresh_seconds）。"""
    now = time.time() if now is None else now
    ts = float(cp.get("updated_at") or 0)
    return bool(ts) and (now - ts) <= resume_fresh_seconds()


def clear_ckpt_slot(user_id: str, turn_id: str) -> None:
    """删除指定轮次的断点槽位（含其 latest 指针指向）。"""
    try:
        with _TURN_CKPT_LOCK:
            path = _turn_ckpt_slot_path(user_id, turn_id)
            if path.exists():
                path.unlink()
            latest = _turn_ckpt_latest_path(user_id)
            if latest.exists():
                try:
                    if latest.read_text(encoding="utf-8").strip() == str(turn_id):
                        latest.unlink()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"清除对话轮断点失败: {e}")


def clear_turn_checkpoint(user_id: str) -> None:
    """清除该用户全部断点槽位（显式作废语义：换角色/换会话/明确放弃任务）。"""
    try:
        with _TURN_CKPT_LOCK:
            for p in _list_ckpt_slot_paths(user_id):
                try:
                    p.unlink()
                except Exception:
                    pass
            latest = _turn_ckpt_latest_path(user_id)
            if latest.exists():
                latest.unlink()
    except Exception as e:
        logger.warning(f"清除对话轮断点失败: {e}")


def _turn_ckpt_still_mine(user_id: str, turn_id: str) -> bool:
    """断点是否仍是当前轮次（防新对话轮覆盖后旧轮误清理/误删消息）。"""
    cp = _read_ckpt_slot(_turn_ckpt_slot_path(user_id, turn_id))
    return bool(cp and cp.get("turn_id") == turn_id)


def _clear_turn_ckpt_if_mine(user_id: str, turn_id: str) -> None:
    if _turn_ckpt_still_mine(user_id, turn_id):
        clear_ckpt_slot(user_id, turn_id)


# ---------- 任务状态摘要（方案 C 兜底：断点丢失也能靠摘要续跑） ----------
TASK_RESUME_FILE = BASE_DIR / "data" / "task_resume_states.json"
_TASK_RESUME_LOCK = threading.Lock()


def _ckpt_summary(cp: dict) -> str:
    """从断点提取任务状态摘要：目标 + 进度 + 待办 + 最近真实工具结果。

    作为断点丢失时的「方案 C 兜底」注入新对话；多带真实工具结果，
    续跑时模型不至于只靠一句话凭空猜测之前做到哪。
    """
    try:
        goal = str(cp.get("user_message") or "").strip()
        if len(goal) > 100:
            goal = goal[:97] + "…"
        done = int(cp.get("tool_round") or 0)
        pending = [str(p.get("name") or p.get("tool") or "?")
                   for p in (cp.get("pending_tools") or [])]
        partial = str(cp.get("assistant_content") or cp.get("full_text") or "").strip()
        if len(partial) > 120:
            partial = partial[:117] + "…"
        # 从检查点消息里提取最近工具执行的真实结果（最多 3 条，各 100 字）
        tool_results = []
        for m in (cp.get("messages") or [])[-10:]:
            if m.get("role") == "tool":
                c = str(m.get("content") or "").strip().replace("\n", " ").replace("\r", " ")
                if c:
                    tool_results.append(c[:100])
            if len(tool_results) >= 3:
                break
        parts = []
        if goal:
            parts.append(f"目标：{goal}")
        if done:
            parts.append(f"已完成 {done} 轮工具调用")
        if pending:
            parts.append("待执行：" + "、".join(pending))
        if partial:
            parts.append(f"已有进展：{partial}")
        if tool_results:
            parts.append("最近工具结果：" + "；".join(tool_results))
        return "；".join(parts) or "（无摘要）"
    except Exception:
        return "（无摘要）"


def save_task_resume_state(user_id: str, summary: str, cp: dict) -> None:
    """打断时记录一句话任务状态（24 小时内有效，供「继续」兜底重建）。"""
    try:
        with _TASK_RESUME_LOCK:
            data = {}
            if TASK_RESUME_FILE.exists():
                try:
                    data = json.loads(TASK_RESUME_FILE.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[str(user_id)] = {
                "summary": summary,
                "ts": time.time(),
                "session_id": str(cp.get("session_id") or ""),
            }
            TASK_RESUME_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = TASK_RESUME_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, TASK_RESUME_FILE)
    except Exception as e:
        logger.warning(f"保存任务状态摘要失败: {e}")


def load_task_resume_state(user_id: str) -> Optional[dict]:
    try:
        if not TASK_RESUME_FILE.exists():
            return None
        data = json.loads(TASK_RESUME_FILE.read_text(encoding="utf-8"))
        st = data.get(str(user_id))
        if st and time.time() - float(st.get("ts") or 0) < 24 * 3600:
            return st
    except Exception:
        pass
    return None


def clear_task_resume_state(user_id: str) -> None:
    try:
        with _TASK_RESUME_LOCK:
            if not TASK_RESUME_FILE.exists():
                return
            data = json.loads(TASK_RESUME_FILE.read_text(encoding="utf-8"))
            data.pop(str(user_id), None)
            tmp = TASK_RESUME_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, TASK_RESUME_FILE)
    except Exception:
        pass


def _mark_ckpt_paused(user_id: str, turn_id: str) -> None:
    """用户打断后把断点标记为「已暂停」，供「继续」指令续跑。

    保留完整现场（消息/工具轮次/待执行工具）不动；同时若有工具活动，
    额外落一句任务状态摘要到独立文件（方案 C 兜底）。
    """
    try:
        cp = _read_ckpt_slot(_turn_ckpt_slot_path(user_id, turn_id))
        if not cp or cp.get("turn_id") != turn_id:
            return
        cp["paused"] = True
        cp["updated_at"] = time.time()
        save_turn_checkpoint(user_id, cp)
        if int(cp.get("tool_round") or 0) > 0 or (cp.get("pending_tools") or []):
            save_task_resume_state(user_id, _ckpt_summary(cp), cp)
    except Exception as e:
        logger.warning(f"标记断点暂停失败: {e}")


def mark_latest_ckpt_paused(user_id: str) -> None:
    """把该用户最新断点标记为「已暂停」（服务端收尾路径兜底用）。

    handle_user_message_stream 的取消分支里，流式生成器可能因
    is_cancelled 检查提前 break 而正常收尾（不会抛 CancelledError 到
    chat_stream），此时由服务端显式标记暂停，保证『继续』可用。
    """
    try:
        cp = load_turn_checkpoint(user_id)
        if not cp or cp.get("paused"):
            return
        cp["paused"] = True
        cp["updated_at"] = time.time()
        save_turn_checkpoint(user_id, cp)
        if int(cp.get("tool_round") or 0) > 0 or (cp.get("pending_tools") or []):
            save_task_resume_state(user_id, _ckpt_summary(cp), cp)
    except Exception as e:
        logger.warning(f"标记最新断点暂停失败: {e}")


def list_turn_checkpoints() -> list:
    """列出全部未完成的对话轮断点（server 启动后据此自主恢复）。"""
    out = []
    for p in _list_ckpt_slot_paths():
        cp = _read_ckpt_slot(p)
        if cp and cp.get("turn_id"):
            out.append(cp)
    return out


def _is_tools_unsupported_error(e: Exception) -> bool:
    """判断是否为"模型不支持工具调用"类错误（如本地 Ollama 模型无 function calling）。"""
    msg = (str(e) or "").lower()
    if "does not support tools" in msg:
        return True
    return "tools" in msg and any(
        key in msg for key in ("not support", "unsupported", "not supported")
    )


def _is_reasoning_echo_error(e: Exception) -> bool:
    """判断是否为「thinking 模式必须回传 reasoning_content」类 400。

    要求报错文本同时点到 reasoning_content 与 passed back/thinking，避免把无关的
    400（参数非法、上下文超长、余额不足）误判成可自愈错误而反复重试。
    """
    msg = (str(e) or "").lower()
    if "reasoning_content" not in msg:
        return False
    return "passed back" in msg or "thinking" in msg


def _is_valid_tool_spec(t) -> bool:
    """校验 OpenAI function-calling 工具 schema 是否合法。

    非法工具（缺 name/description、parameters 不是 object、name 含非法字符等）
    会导致整个请求被提供方以 400「parameter invalid」拒绝（表现为整轮开小差），
    这里在发送前拦截，返回 False 的不注入可调用列表。
    """
    if not isinstance(t, dict) or t.get("type") != "function":
        return False
    fn = t.get("function")
    if not isinstance(fn, dict):
        return False
    name = fn.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        return False
    if not isinstance(fn.get("description"), str) or not fn.get("description").strip():
        return False
    params = fn.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        return False
    return True


def _tool_names(tools: list) -> list:
    """工具名清单（诊断用；顺序即序列化顺序，顺序本身就是缓存键的一部分）。"""
    return [str(((t or {}).get("function") or {}).get("name") or "")
            for t in (tools or [])]


def _tools_chars(tools: list) -> int:
    """工具 schema 总字符数（与 prefix_probe 同算法，便于对账）。"""
    try:
        return sum(len(json.dumps(t, ensure_ascii=False, sort_keys=True))
                   for t in (tools or []))
    except Exception:
        return 0


def _trace_tools_change(reason: str, names: list, chars: int = 0) -> None:
    """工具集变化落盘（data/tools_trace.jsonl）。

    tools 排在请求最前，它一变整条前缀全废——而请求侧只看得见「结果变了」，
    看不见「谁改的」。所以三个改动点各自留一行：activate / evict / reset。
    纯诊断：失败静默，绝不参与消息构造。

    自检（tag=test）不写：这个文件是排查真实缓存的依据，而自检每跑一次就会
    触发一串 reset/activate——混进去后「真实环境里谁在改工具集」直接不可读
    （实测踩过：40 条 reset 里只有 4 条是真的，剩下的全是自检回放）。
    """
    if not _skills_state_persist_enabled():
        return
    try:
        rec = {"ts": time.time(), "reason": reason, "count": len(names),
               "chars": chars, "names": names, "tag": _trace_tag()}
        path = BASE_DIR / "data" / "tools_trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_local_tools() -> list:
    """从 tools.json 加载本地工具定义，并合并 harness 技能/插件的工具。

    返回 OpenAI function calling 格式的工具列表。harness 加载失败不影响原有工具。
    所有工具在并入前逐一校验 schema，非法定义跳过并告警（防止热重载窗口期
    的半成品工具 schema 拖垮整轮请求）。
    """
    tools_path = BASE_DIR / "tools.json"
    all_tools = []
    dropped = []
    if tools_path.exists():
        try:
            with open(tools_path, "r", encoding="utf-8") as f:
                tools_data = json.load(f)
            for tool_list in tools_data.values():
                if isinstance(tool_list, list):
                    all_tools.extend(tool_list)
        except Exception as e:
            logger.warning(f"加载本地工具失败: {e}")

    # harness 技能/插件工具（动态扩展；失败只告警，不影响原工具）
    try:
        from harness import get_harness
        seen = {t.get("function", {}).get("name") for t in all_tools if t.get("function", {}).get("name")}
        for t in get_harness().collect_tool_specs() or []:
            name = (t.get("function") or {}).get("name")
            if name and name not in seen:
                seen.add(name)
                all_tools.append(t)
    except Exception as e:
        logger.warning(f"加载 harness 技能/插件工具失败: {e}")

    # 发送前统一校验：非法工具直接剔除，绝不让一个坏 schema 弄挂整轮请求
    sanitized = []
    for t in all_tools:
        if _is_valid_tool_spec(t):
            sanitized.append(t)
        else:
            name = (t.get("function") or {}).get("name") if isinstance(t, dict) else "?"
            dropped.append(name)
    if dropped:
        logger.warning(f"跳过 %d 个非法工具 schema（防 400 parameter invalid）: %s",
                       len(dropped), ", ".join(str(x) for x in dropped[:20]))
    return sanitized


def load_agent_tool_config(user_id: str = "") -> tuple:
    """动态读取该用户生效的工具配置（卡片 tools 优先，回落 settings.json）。

    Returns:
        (enable_tools, allowed_tools): 是否启用工具 + 允许的工具名白名单（空列表=全部可用）
    """
    try:
        cfg = load_config_for(user_id) if user_id else load_config()
        agent_cfg = cfg.get("agent", {})
        enable_tools = bool(agent_cfg.get("enable_tools", True))
        allowed_tools = agent_cfg.get("allowed_tools", []) or []
        return enable_tools, allowed_tools
    except Exception:
        return True, []


def get_available_tools() -> list:
    """返回所有可用本地工具（名称 + 描述），供前端角色卡片配置工具白名单。

    工具列表来自本地工具 + harness 技能/插件（全部 skill 化）。
    """
    tools = []
    seen = set()
    for t in load_local_tools():
        fn = t.get("function", {})
        name = fn.get("name", "")
        if name and name not in seen:
            seen.add(name)
            tools.append({
                "name": name,
                "description": fn.get("description", ""),
                "source": "local",
            })
    return tools


def _harness_base():
    """harness 数据目录：默认项目根，harness 注册了自定义 base_dir 时以它为准。"""
    from pathlib import Path as _Path
    base = _Path(__file__).resolve().parent
    try:
        from harness import get_harness
        base = _Path(getattr(get_harness(), "base_dir", base))
    except Exception:
        pass
    return base


def _gene_mod():
    """加载 tools/gene_fitness.py：键算法与曝光统计的唯一真源。失败返回 None。"""
    try:
        import importlib.util as _ilu
        f = _harness_base() / "tools" / "gene_fitness.py"
        spec = _ilu.spec_from_file_location("gene_fitness", f)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# 本轮已进 prompt 的基因键（"kind:sha1"）与首次注入时刻：轮末由 _gene_flush_exposure
# 写一行曝光流水。键集合是「基因 ↔ 结局」对照的唯一凭据，累计次数反推不出它。
_GENE_ROUND_KEYS: list = []
_GENE_ROUND_TS: str = ""
# 留空轮换序号：每轮 +1，决定 longterm/conviction 段本轮留空哪一条。模块加载时取
# 时间低位做起点，进程重启后不会从同一序列重放（否则重启越勤、留空越集中）。
_GENE_HOLDOUT_SEQ: int = int(time.time()) & 0xFFFF
# 上一次曝光流水落盘的时刻：结局窗口的左端。终态事件只归到「它到达终态的那一轮」，
# 窗口必须首尾相接——用固定轮长会把跨轮完成的长期任务漏掉。
_GENE_LAST_TS: float = 0.0


def _gene_round_reset() -> int:
    """丢弃本轮未落盘的缓冲，返回被丢弃的键数。测试隔离与异常兜底用。"""
    global _GENE_ROUND_TS, _GENE_HOLDOUT_SEQ
    n = len(_GENE_ROUND_KEYS)
    _GENE_ROUND_KEYS.clear()
    _GENE_ROUND_TS = ""
    _GENE_HOLDOUT_SEQ += 1
    return n


def _gene_touch(pairs) -> None:
    """把本轮真正进了 prompt 的基因各记一次曝光，供 tools/gene_fitness.py 算选择压力。

    直接 exec tools/gene_fitness.py 而不是在本地重算 sha1 键：键算法两份实现一旦
    漂移，埋点会静默归零——那正是这次要修的故障本身。全程静默，埋点失败不影响对话。
    累计次数即时写盘（够做轮换排序），键集合留在缓冲等轮末一次性落盘。
    """
    mod = _gene_mod()
    if mod is None:
        return
    try:
        mod.touch_batch(pairs)
    except Exception:
        pass
    try:
        global _GENE_ROUND_TS
        if not _GENE_ROUND_TS:
            _GENE_ROUND_TS = time.strftime("%Y-%m-%d %H:%M:%S")
        for p in (pairs if isinstance(pairs, list) else []):
            if isinstance(p, dict):
                kind, text = p.get("kind", ""), p.get("text", "")
            else:
                kind, text = p[0], p[1]
            k = f"{kind}:{mod.key_of(text)}"
            if k not in _GENE_ROUND_KEYS and len(_GENE_ROUND_KEYS) < 200:
                _GENE_ROUND_KEYS.append(k)
    except Exception:
        pass


def _gene_flush_exposure(metrics: dict | None = None) -> int:
    """把本轮注入过的基因键写成一行曝光流水（一轮一行），返回写入行数。

    挂在 _record_turn_metrics 最前面——那是「本轮结束」的必经点，且在 tool_round
    判断之前，没调工具的轮次也照样记账。缓冲先取走再写：写盘失败不重复投递。

    metrics 是本轮结局标签（工具轮数/报错数/耗时 + 本轮到达终态的任务结局）：keys 只
    回答「谁在场」，配上结局才能问「它在场的那一轮，是变好了还是没变」。
    """
    if not _GENE_ROUND_KEYS:
        return 0
    keys, turn = list(_GENE_ROUND_KEYS), _GENE_ROUND_TS
    _gene_round_reset()
    mod = _gene_mod()
    if mod is None:
        return 0
    global _GENE_LAST_TS
    now = time.time()
    try:
        # 窗口 = 上轮落盘时刻 → 现在。首轮没有上轮，退化成「本轮时长」而不是从 0 起算
        # ——从 0 起算会把历史上所有终态任务一次性算进本轮，那列结局就永久失真。
        start = _GENE_LAST_TS or (now - float((metrics or {}).get("duration_ms") or 0) / 1000.0)
        oc = mod.task_outcomes(start, now)
        if oc and isinstance(metrics, dict):
            metrics = dict(metrics)
            metrics["task_outcomes"] = oc
    except Exception:
        pass
    try:
        n = mod.log_exposure(keys, turn, metrics=metrics)
    except Exception:
        return 0
    if n:
        # 只有真写进去了才推进窗口：写盘失败时下次窗口自动扩大，宁可重收也不能漏。
        _GENE_LAST_TS = now
    return n


def _gene_pick(items: list, cap: int, fresh: int = 2) -> list:
    """注入窗口选取：前 fresh 条保新近性（刚踩的坑立刻能用），其余按曝光升序补足。

    为什么不按命中降序：inject 记的是曝光次数（自变量），不是价值（因变量）。按曝光
    降序排会让已在窗口的 6 条永久霸占窗口，窗口外 54 条永远拿不到验证机会。最少曝光
    优先是自动轮换——每轮注入都 +1，下一轮自然轮到别的未验证基因。
    """
    ls = [str(x) for x in (items if isinstance(items, list) else [])]
    head, rest = ls[:fresh], ls[fresh:]
    mod = _gene_mod()
    if mod is not None:
        try:
            rest = mod.order_by_exposure(rest)
        except Exception:
            pass
    return (head + rest)[:cap]


def _gene_holdout(items: list, cap: int) -> list:
    """给「恒在场」的基因造缺席轮次：每轮轮换留空 1 条，返回本轮实际注入的列表。

    为什么必须留空：实测 5 条基因（3 条 longterm + 2 条 conviction）4/4 轮 100% 在场
    ——cap 大于候选数，它们结构上不可能缺席。没有缺席就没有对照组，效应在数学上
    不可识别：这不是样本量不够，是实验设计缺一块，攒到 1000 轮也估不出。

    留空对象按轮次严格轮换（seq % n），长期看每条被留空次数均衡；seq 轮内固定、
    轮末递增，同一轮多次构建 prompt 结果一致。不留开关：半开半关的轮次混在一起，
    效应估计更乱，要停就整段回退。
    """
    ls = list(items) if isinstance(items, list) else []
    if len(ls) <= 1:
        return ls[:cap]
    k = _GENE_HOLDOUT_SEQ % len(ls)
    return [x for i, x in enumerate(ls) if i != k][:cap]


# 注入段的收尾标点：截断必须停在句子边界
_CLIP_PUNC = "。；！？，、）】」"


def _clip(text, n: int, tol: float = 0.5) -> str:
    """把注入文本截到 n 字符量级，尽量停在标点上；实际长度落在 n~1.5n。

    为什么不直接 t[:n]：硬切会把 91% 的教训砍在句中（实测 68 条里 62 条），注入的是
    一句读不懂的残句——那条教训等于白注入，选择压力和 fitness 都建立在噪声上。
    句子完整优先于字符数，多出的几十字符比一整条读不懂的基因便宜。
    """
    s = str(text)
    if len(s) <= n:
        return s
    cut = s[:n]
    i = max((cut.rfind(p) for p in _CLIP_PUNC), default=-1)
    if i >= n * tol:
        return cut[:i + 1]
    over = s[n:max(n + 1, int(n * 1.5))]
    j = min((k for k in (over.find(p) for p in _CLIP_PUNC) if k >= 0), default=-1)
    return s[:n + j + 1] if j >= 0 else cut + "…"


# 教训里「能改变下次行为」的部分：规则句优先于背景句
_LESSON_RULE = re.compile(r"(必须|不要|别|规则：|结论：|判据|否则|禁止|只能|优先|先.{0,8}再)")
_SENT_SPLIT = re.compile(r"(?<=[。；！？])")


def _clip_lesson(text, n: int = 150) -> str:
    """经验库注入专用的截断：保住「规则句」，不是保住「背景句」。

    背景（2026-09-14）：教训的写法是「背景 → 实测 → 规则」，而 _clip 取前 120 字符
    必然取到背景，规则永远进不来——实测 77 条里 47 条（61%）的指令词只出现在第 120
    字符之后，注入的是一条读得懂、但没法据以行动的故事。这里把同一份字符预算换成
    「主题句 + 规则句」：平均注入长度 107→117 字符，真库丢规则 23→0 条。
    无规则句时退回 _clip，不引入新的失败模式。
    """
    s = str(text)
    if len(s) <= n:
        return s
    sents = [x for x in _SENT_SPLIT.split(s) if x.strip()]
    if len(sents) < 2:
        return _clip(s, 120)
    head = sents[0]
    if len(head) > n * 0.7:
        head = _clip(s, int(n * 0.6))
    for x in sents[1:]:
        if not _LESSON_RULE.search(x):
            continue
        room = n - len(head)
        if len(x) <= room:
            return head + x
        if room >= 30:
            return head + _clip(x, room)
        break
    return _clip(s, 120)


def _harness_lessons_block(cap: int = 6) -> str:
    """读 harness 经验库（跨任务踩坑记录），生成对话层可注入的经验段；无经验返回空串。

    背景（2026-09-12）：harness/tasks.py:732 的 _remember_lesson / _lessons_prompt 早已
    存在，但全项目除 tasks.py 自身外零调用，且只注入给任务规划器/反思器
    （tasks.py:423、1334）；harness_task_memory.json 全盘不存在——这台学习机从未通电。
    这里把读出端接进主对话。放在易变尾巴而非静态前缀：经验一变就会作废其后全部缓存。
    """
    try:
        from pathlib import Path as _Path
        base = _Path(__file__).resolve().parent
        try:
            from harness import get_harness
            base = _Path(getattr(get_harness(), "base_dir", base))
        except Exception:
            pass
        f = base / "harness_task_memory.json"
        if not f.exists():
            return ""
        data = json.loads(f.read_text(encoding="utf-8"))
        ls = data.get("lessons") if isinstance(data, dict) else None
        if not isinstance(ls, list) or not ls:
            return ""
        pick = _gene_pick(ls, cap)
        lines = "\n".join(f"- {_clip_lesson(x)}" for x in pick)
        _gene_touch([("lesson", str(x)) for x in pick])
        return "【历史经验（此前踩过的坑/成功路径，来自 harness 经验库）】\n" + lines
    except Exception:
        return ""


def _harness_longterm_block(cap: int = 3, cap_q: int = 3) -> str:
    """读长期事业台账，生成「未完成的事业」注入段；无进行中项目返回空串。

    背景（2026-09-12）：大白的每轮对话都是新生，任务做完即焚——没有跨会话的目标、
    进度和接力棒，「长期深耕」在结构上就不可能发生。人能深耕靠的不是动力这种玄学，
    是连续性：昨天的进展今天还在、有未完成的目标追着、能看见自己变强。本段注入这
    三件事，紧邻经验库段放在易变尾巴（一变只报废自身缓存）。写入端 tools/long_horizon.py。
    """
    try:
        from pathlib import Path as _Path
        base = _Path(__file__).resolve().parent
        try:
            from harness import get_harness
            base = _Path(getattr(get_harness(), "base_dir", base))
        except Exception:
            pass
        f = base / "long_horizon.json"
        if not f.exists():
            return ""
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        projs = [x for x in (data.get("projects") or []) if x.get("stage") == "active"]
        qs = [x for x in (data.get("questions") or []) if x.get("status") == "open"]
        if not projs and not qs:
            return ""
        picked = _gene_holdout(projs, cap)
        out = ["\n【长期事业（跨会话持续，开工先看这里）】"]
        for x in picked:
            head = f"▸ {x.get('title') or x.get('id')} [{x.get('progress', 0)}%]"
            if x.get("value"):
                head += f" 价值：{_clip(x['value'], 60)}"
            out.append(head)
            if x.get("next"):
                out.append(f"  下一步（上一轮留的接力棒）：{x['next']}")
            lg = x.get("log") or []
            if lg:
                out.append(f"  最近：{lg[0].get('t', '')} {_clip(lg[0].get('what', ''), 60)}")
        if qs:
            out.append("悬而未决（创新燃料，遇到相关信息就碰一下）：")
            out.extend(f"- {_clip(x.get('text', ''), 80)}" for x in qs[:cap_q])
        out.append(
            "推进了就立刻落盘：`venv/bin/python tools/long_horizon.py log <id> \"做了什么\" --ev 证据`；"
            "收工前把 next 改写成下一轮能直接开跑的原子动作。\n"
        )
        _gene_touch([("longterm", str(x.get("title") or x.get("id"))) for x in picked])
        return "\n".join(out)
    except Exception:
        return ""


def _harness_conviction_block(cap: int = 4) -> str:
    """读主体性台账（信条 + 最近拒绝），生成对话层可注入段；空则返回空串。

    背景（2026-09-12）：规则是「别人要我怎么做」，信条是「我自己判断该怎么做」。
    一个从不拒绝任何东西的智能体没有主体性，只是更快的执行器。本段把「我信什么、
    我拒绝过什么」注入对话，和 lessons（坑→怎么做）、long_horizon（我要去哪）并列。
    写入端 tools/conviction.py，同样放易变尾巴（信条/拒绝一变只报废自身缓存）。
    """
    try:
        from pathlib import Path as _Path
        base = _Path(__file__).resolve().parent
        try:
            from harness import get_harness
            base = _Path(getattr(get_harness(), "base_dir", base))
        except Exception:
            pass
        f = base / "conviction.json"
        if not f.exists():
            return ""
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
        cs = data.get("convictions") or []
        vs = data.get("vetoes") or []
        if not cs and not vs:
            return ""
        picked = _gene_holdout(cs, cap)
        out = ["【信条与拒绝（我自己判断该怎么做，可被挑战，过不了就降级或删）】"]
        for c in picked:
            out.append(f"- {c.get('text', '')}")
        if vs:
            out.append("最近拒绝过（主体性的直接证据）：")
            out.extend(f"- {_clip(v.get('claim', ''), 60)}" for v in vs[:2])
        _gene_touch([("conviction", str(c.get("text", ""))) for c in picked])
        return "\n".join(out)
    except Exception:
        return ""



def _harness_peer_block(preview: int = 2) -> str:
    """联邦收件箱未读：别的机器上的大白留的话。没有未读返回空串。

    背景（2026-09-18）：联邦实时电话已上线，但收件箱只有我主动调 peer_inbox 才看得见——
    对话轮里没有任何「有信到了」的信号，等于信寄到了没人拆。本段只报未读数 + 最近几条
    摘要，不标已读（标已读留给真正读信的动作），放在易变尾巴。
    """
    try:
        import time as _t
        import peer_mesh as pm
        s = pm.unread_summary(preview=preview)
        n = int(s.get("count") or 0)
        if n <= 0:
            return ""
        out = [f"【联邦来信（其他机器上的大白留的话）】未读 {n} 条 —— 调 peer_inbox 读全文（读完自动标已读）。"]
        for m in s.get("items") or []:
            when = _t.strftime("%m-%d %H:%M", _t.localtime(m.get("ts", 0)))
            out.append(f"- [{m.get('from', '?')} {when}] {_clip(str(m.get('text', '')), 60)}")
        return "\n".join(out)
    except Exception:
        return ""

def get_harness_prompt_extras(active_skills=None) -> str:
    """返回 harness 技能/插件注入 system prompt 的提示词片段（动态能力说明）。

    渐进披露开启时，未激活的 on_demand 技能不逐条常驻，只汇总技能名清单；
    已激活技能注入一句话摘要——与工具注册同步。失败时静默返回空串。
    """
    try:
        from harness import get_harness
        return get_harness().collect_prompt_extras(active_skills)
    except Exception as e:
        logger.warning(f"获取 harness 提示词片段失败: {e}")
        return ""


def _media_workers_status_text() -> str:
    """当前正在干活的媒体子智能体摘要（主智能体每轮都能看到有哪些子进程在干活）。

    主智能体据此回答「有什么在播/有谁在干活」；失败时静默返回空串。
    """
    try:
        from media_workers import get_media_workers
        return get_media_workers().active_text()
    except Exception as e:
        logger.warning(f"获取媒体子智能体状态失败: {e}")
        return ""


def _video_status_text() -> str:
    """当前在播视频摘要（注入 system prompt，角色每轮都能看到用户在看什么）。

    用户在大屏点播/停止时 video_lib.STATE["now"] 实时更新（play API 与
    control stop 都会写），这里每轮对话取一次快照。没有在播返回空串。
    与 server.py _video_lib() 共享同一模块实例（sys.path 同路径 + 模块缓存）。
    """
    try:
        import sys as _sys
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "skills", "video")
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
        import video_lib as _vl
        st = _vl.public_state()
        now = st.get("now")
        if not now:
            return ""
        title = (now.get("title") or "").strip()
        if not title:
            return ""
        bits = []
        uploader = (now.get("uploader") or "").strip()
        platform = (now.get("platform") or "").strip()
        if uploader:
            bits.append(uploader)
        if platform:
            bits.append(platform)
        dur = now.get("duration")
        if isinstance(dur, (int, float)) and dur > 0:
            m, s = int(dur) // 60, int(dur) % 60
            bits.append(f"{m}分{s:02d}秒" if m else f"{s}秒")
        info = "·".join(bits)
        pl = st.get("player") or {}
        paused = "（已暂停）" if pl.get("paused") else ""
        q = st.get("queue") or []
        qtxt = f"，连播队列还有{len(q)}部" if q else ""
        return f"视频：正在播放《{title}》{paused} {info}{qtxt}"
    except Exception as e:
        logger.warning(f"获取在播视频状态失败: {e}")
        return ""


def _sub_agents_status_text() -> str:
    """当前正在干活的通用子智能体摘要（下发任务后主智能体每轮都能看到子进程）。"""
    try:
        from sub_agents import get_sub_agents
        return get_sub_agents().active_text()
    except Exception as e:
        logger.warning(f"获取通用子智能体状态失败: {e}")
        return ""


def _get_runtime():
    """取 harness 监督运行时（AgentRuntime）；harness 不可用时返回 None。

    Agent 的所有 LLM 调用与工具执行都经它监督（重试/超时/熔断/计量），
    拿不到运行时则退化为原有裸调用行为。
    """
    try:
        from harness import get_harness
        return get_harness().runtime
    except Exception:
        return None


async def execute_local_tool(tool_name: str, arguments: dict) -> str:
    """执行本地/内置工具。

    Args:
        tool_name: 工具名称
        arguments: 工具参数字典

    Returns:
        工具执行结果字符串。屏幕控制类工具返回带 __screen_command__ 前缀的 JSON。
    """
    # ============ 原内置工具已迁移为「渐进式披露技能」（skills/ 与 plugins/，经 harness 路由） ============
    # 屏幕控制（换装/换场景/换声/模式/Toast/BGM/游戏）、智能体委派（dsh/codex/opencode）、
    # 任务查询等全部由 skills/{appearance,voice,music,interface,agent_ops} 提供，
    # 执行结果（__screen_command__ / __dsh_bridge__ / __codex_delegate__ 标记 JSON）与原来完全一致。

    # ============ harness 技能 / 插件工具（稳定路由） ============
    try:
        from harness import get_harness
        result, source = await get_harness().execute_tool(tool_name, arguments)
        if result is not None:
            return result
    except Exception as e:
        return f"执行 harness 工具 '{tool_name}' 时出错: {e}"

    # 从 fuctions_all_you_need_base 加载的工具函数（最后兜底）
    try:
        import importlib
        from fuctions_all_you_need_base import excute_functions
        result = excute_functions(name=tool_name, args=json.dumps(arguments, ensure_ascii=False))
        return str(result)
    except ImportError:
        return f"工具 '{tool_name}' 未找到实现"
    except Exception as e:
        return f"执行工具 '{tool_name}' 时出错: {e}"


class ToolCallEvent:
    """工具调用事件的基类。"""


class TextDelta(ToolCallEvent):
    """文本增量事件。"""
    def __init__(self, text: str):
        self.text = text


class ThinkingDelta(ToolCallEvent):
    """思维链增量事件：工具执行过程的过程话（只进思考段展示，不朗读）。"""

    def __init__(self, text: str):
        self.text = text


class StreamDelta(ToolCallEvent):
    """实时文本增量事件：LLM 生成过程中逐段流出（展示 + 语音即时跟随）。

    与 TextDelta 的区别：StreamDelta 不进入服务端 full_text（最终历史/结论
    由 FinalText 单独提供），工具轮的过程话会被前端转入思考段。
    """
    def __init__(self, text: str):
        self.text = text


class ReasoningDelta(ToolCallEvent):
    """真实推理步骤事件（reasoning_content）：只进思考段展示，不进正文、不朗读。"""
    def __init__(self, text: str):
        self.text = text


class TurnStatus(ToolCallEvent):
    """对话轮状态事件：网络波动自动重连等运行状态提示。
    前端在回合气泡底部一行展示，不打断流程、不朗读、不进正文。"""
    def __init__(self, text: str):
        self.text = text


class FinalText(ToolCallEvent):
    """最终回复全文事件：只用于服务端记录 full_text（历史/audio_end 全文），
    展示与语音已在生成过程中经 StreamDelta 实时流出，无需重复推送。"""
    def __init__(self, text: str):
        self.text = text


class ToolCallStart(ToolCallEvent):
    """工具调用开始事件。"""
    def __init__(self, tool_name: str, arguments: str, tool_desc: str = ""):
        self.tool_name = tool_name
        self.arguments = arguments
        # 工具**自己**的说明（技能/插件里 function.description 的第一句）。
        # 前端中央字幕直接显示它，而不是维护一张「工具名 → 人话」的映射表——
        # 工具是动态扩展的（技能按需加载、插件、用户自建），映射表必然过期，
        # 新工具落进兜底分支，说明就变得不伦不类。
        # 工具描述由工具作者写、和工具同生共死，是唯一不会过期的来源。
        self.tool_desc = tool_desc


class ToolCallResult(ToolCallEvent):
    """工具调用结果事件。"""
    def __init__(self, tool_name: str, result: str, success: bool = True):
        self.tool_name = tool_name
        self.result = result
        self.success = success


class ToolCallProgress(ToolCallEvent):
    """工具执行心跳事件：工具仍在执行时周期性发出，让用户知道任务没有卡死。
    用于长任务（如 shell 长命令、文件搜索、媒体处理）的执行反馈。"""
    def __init__(self, tool_name: str, elapsed: float, message: str = ""):
        self.tool_name = tool_name
        self.elapsed = elapsed
        self.message = message


class UsageEvent(ToolCallEvent):
    """LLM 用量事件：一轮对话结束后发出一次（真实 usage 数据）。

    两个 prompt 口径必须分开，不能混用（本项目实际踩过）：
      · prompt_tokens —— 本轮**所有** LLM 调用的输入之和。只有它满足
        prompt + completion == total，也只有它适合「累计输入」这类统计；
      · context_tokens —— 本轮**最后一次**调用的 prompt，即「当前上下文有多大」，
        用于上下文占用仪表（占用率 = context_tokens / context_window）。
    曾把 last_prompt 当作 prompt_tokens 发出：一个 7 次调用的轮次会显示
    「输入 73.3k + 输出 3.3k，共 409.4k」——加法不成立，且命中率算出 378%。

    cache_hit/cache_miss 为提供方前缀缓存计费口径（如 DeepSeek
    prompt_cache_hit_tokens / prompt_cache_miss_tokens），不支持时为 0。
    """
    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                 rounds=0, context_window=0,
                 cache_hit_tokens=0, cache_miss_tokens=0, context_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.rounds = rounds
        self.context_window = context_window
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.context_tokens = context_tokens


class AgentResponse:
    """Agent 响应的完整结果。"""
    def __init__(self):
        self.text = ""
        self.tool_calls_made: list = []  # [(tool_name, arguments, result), ...]


def _model_first_transport(inner):
    """包装 httpx transport：把 chat.completions 请求体的 JSON 键重排，
    让 "model" 总是排在最前。

    为什么需要：opencode.ai/zen 网关的请求解析器有 bug —— 它按 JSON 键顺序
    读 model，openai SDK 序列化时 "messages" 在 "model" 之前，网关就会认为
    请求没有 model，返回 401 ModelError "Model  is not supported"（表现为
    聊天里"（AI 暂时开小差了：...）"）。裸 httpx（"model" 在前）一直正常。
    这里在传输层把键顺序纠正过来，无论 SDK 怎么序列化都能正常通过。
    """
    import json as _json
    import httpx as _httpx

    class _ModelFirstTransport(_httpx.AsyncBaseTransport):
        def __init__(self, _inner_):
            self._inner = _inner_

        async def handle_async_request(self, request):
            content = request.content
            if request.method == "POST" and content:
                try:
                    obj = _json.loads(content.decode("utf-8"))
                    if isinstance(obj, dict) and "model" in obj:
                        ordered = {"model": obj["model"]}
                        for k, v in obj.items():
                            if k != "model":
                                ordered[k] = v
                        new_body = _json.dumps(ordered, ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")
                        headers = dict(request.headers)
                        headers["content-length"] = str(len(new_body))
                        request = _httpx.Request(request.method, request.url,
                                                 headers=headers, content=new_body)
                except Exception:
                    pass  # 非 JSON / 解析失败：原样透传，绝不影响请求
            return await self._inner.handle_async_request(request)

    return _ModelFirstTransport(inner)


def _build_llm_client(base_url: str, api_key: str, proxy: str | None = None) -> AsyncOpenAI:
    """构建 LLM 客户端（可走 fq 代理）。

    默认 trust_env=True 会让 httpx 读取 Windows 系统代理（注册表
    HKCU/Software/Microsoft/Windows/CurrentVersion/Internet Settings）。
    当系统代理指向未运行的本地端口时（例如 127.0.0.1:31181 无监听），
    所有 https 请求都会连接失败，表现就是聊天里"（AI 暂时开小差了：...）"。
    这里显式传入 trust_env=False 的 httpx 客户端，让 LLM 调用只走
    显式指定的 proxy（settings.json 的 llm_proxy：auto=自动拉起 fq 代理；
    自定义地址；缺省直连），绝不信任系统代理。
    """
    import httpx as _httpx
    inner = _httpx.AsyncHTTPTransport(proxy=proxy) if proxy else _httpx.AsyncHTTPTransport()
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=_httpx.AsyncClient(
            transport=_model_first_transport(inner),
            trust_env=False,
            timeout=_httpx.Timeout(120.0, connect=30.0),
        ),
    )


def _resolve_client_proxy(cfg: dict) -> str | None:
    """按 settings.json 解析 LLM 客户端代理；本地地址（Ollama 等）永远直连。"""
    if is_local_url(str(cfg.get("base_url") or "").strip()):
        return None
    return resolve_llm_proxy(cfg)


class AIAgent:
    """AI Agent —— 具备工具调用和长期记忆能力的对话代理。

    使用方式:
        agent = AIAgent(user_id="user123")
        async for event in agent.chat_stream("帮我查一下天气", history=None):
            if isinstance(event, TextDelta):
                print(event.text, end="")
            elif isinstance(event, ToolCallStart):
                print(f"\n🔧 调用工具: {event.tool_name}")
            elif isinstance(event, ToolCallResult):
                print(f"📋 结果: {event.result[:200]}")
    """

    def __init__(self, user_id: str = "default", namespace: str = ""):
        self.user_id = user_id
        # 命名空间覆盖：无头调用方（长跑引擎）用它落到独立会话。
        # 否则会 adopt 角色卡的活跃会话——读走整段历史，还把自己的输出写回去。
        self._ns_override = namespace.strip()
        self.memory: Optional[ChatMemory] = None
        self._client: Optional[AsyncOpenAI] = None
        self._config: dict = {}
        self._all_tools: list = []
        self._base_tools: list = []  # 静态基础工具缓存（本地 + full 披露技能 + skill_help）
        self._local_tool_names: set = set()
        self._activated_skills: set = set()  # 渐进式披露：已通过 skill_help 按需加载的技能
        self._skill_last_used: dict = {}     # 技能最近使用时间（轮内上限淘汰用）
        self._skill_order: list = []         # 技能激活先后（重挂顺序的唯一依据）
        self._skills_restored_sid = None     # 已评估过技能恢复的 sid（重启重挂用）
        self._initialized = False
        self._text_tool_mode = False  # 当前模型不支持原生工具调用时置 True，改用文本协议
        self._reasoning_echo_required = False  # thinking 渠道要求回传 reasoning_content：踩过一次后每轮前置补齐
        self._usage_enabled = True  # LLM 用量统计开关（运行时提供方不支持时可降级为 False）
        self._last_round_fps: list = []  # 死循环防护：最近几轮工具调用指纹
        self._last_round_sigs: list = []  # 死循环防护（强判据）：对应轮次的结果指纹
        self._loop_warn_count = 0   # 已注入的循环提醒次数（连续命中仍未收敛才硬停）
        # 跨轮工具调用指纹：eff_seen 只覆盖单轮，模型「第 3 轮读过、第 7 轮又读」
        # 这种跨轮重复完全不被统计——而这恰恰是长任务里最典型的浪费。
        # 只用于统计，不做任何拦截（用户明确要求重读时不该被拦）。
        # 用 dict 而非 set：需要按插入顺序淘汰旧项，set 无序做不到。
        self._cross_round_fps: dict = {}
        # 「连续单发只读」：连续几轮各只发 1 个只读工具（每轮都付一次 LLM 往返）。
        # 与 _cross_round_fps 的分工：那个看「同一个调用被重复」，这个看「本可并行的
        # 调用被拆到多轮」——实测每 LLM 轮只发 1.36 个工具，跨轮串行才是主要浪费。
        self._single_ro_streak = 0
        self._single_ro_names: list = []
        self._single_ro_armed = False   # 本轮已判定该给反馈，由结果构造处消费
        # 实例标识：写进诊断日志，用来区分「视图被清」和「换了实例」
        # （真实样本里同一 sid 反复 reset_sid，只有这个能证伪）。
        self._inst_tag = f"{os.getpid()}-{id(self) & 0xFFFF:04x}"
        _trace_hist_view("", 0, 0, -1, "instance_new", 0, self._inst_tag)

    async def initialize(self):
        """初始化 Agent：加载配置、加载技能工具、初始化记忆。"""
        if self._initialized:
            return

        self._config = load_config_for(self.user_id)

        # 初始化 OpenAI 客户端（可走 fq 代理，不信任系统代理）
        self._client = _build_llm_client(self._config["base_url"], self._config["api_key"],
                                         _resolve_client_proxy(self._config))

        # 收集所有可用工具（本地工具 + harness 技能/插件工具，全部 skill 化）
        local_tools = self._filtered_tools()
        self._all_tools = local_tools
        self._base_tools = list(local_tools)

        # 记录本地工具名（用于路由执行）
        for t in local_tools:
            self._local_tool_names.add(t["function"]["name"])

        logger.info(
            f"Agent 初始化完成: 共 {len(local_tools)} 个工具"
            f"（本地 + harness 技能/插件，已全面 skill 化）"
        )

        # 初始化记忆（绑定当前活动角色卡片对应的独立记忆命名空间）
        self.memory = ChatMemory(user_id=self.user_id, namespace=self._active_memory_namespace())
        self.memory.set_llm_client(self._client, self._config["model"])
        await self.memory.get_or_create_session()

        # 注册进 harness 监督运行时（此后全部 LLM/工具调用受监督）
        runtime = _get_runtime()
        if runtime is not None:
            runtime.register_agent(self.user_id,
                                   model=self._config.get("model", ""),
                                   base_url=self._config.get("base_url", ""))
        # 注册 harness 任务系统的 LLM 执行器（长任务流程的 llm 步骤经此调用，走 plan 渠道监督）
        try:
            from harness import get_harness as _gh
            _gh().tasks.set_llm_executor(self._harness_task_llm)
        except Exception as e:
            logger.warning(f"注册任务系统 LLM 执行器失败: {e}")

        self._initialized = True

    async def _harness_task_llm(self, system: str, prompt: str,
                                max_tokens: int = 800, temperature: float = 0.3) -> str:
        """harness 任务系统 llm 步骤的执行器：受监督的非流式调用（plan 渠道）。"""
        resp = await self._retry_create(
            kind="plan",
            model=self._config.get("model", ""),
            messages=[
                {"role": "system", "content": system or "你是任务执行助手，简洁准确地完成给定步骤。"},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        u = self._read_usage(resp)
        if u:
            runtime = _get_runtime()
            if runtime is not None:
                ch, cm = self._read_cache(getattr(resp, "usage", None))
                runtime.record_usage("plan", u[0], u[1], u[2],
                                     cache_hit=ch, cache_miss=cm)
        return (resp.choices[0].message.content or "").strip()

    async def reload_llm_config(self):
        """重载 LLM 配置（base_url / api_key / model），使角色卡片切换后的模型即时生效。

        角色卡片可配置独立的大语言模型；切换卡片后调用本方法重建 OpenAI 客户端，
        同时刷新记忆模块使用的模型名。
        """
        try:
            self._config = load_config_for(self.user_id)
            self._client = _build_llm_client(
                self._config.get("base_url", ""),
                self._config.get("api_key", ""),
                _resolve_client_proxy(self._config),
            )
            # 提供方切换后重新探测：云端模型（支持原生工具）不再走文本协议
            self._text_tool_mode = False
            if self.memory:
                self.memory.set_llm_client(self._client, self._config.get("model", ""))
            # 同步更新监督运行时里的注册信息（模型/提供方已变）
            runtime = _get_runtime()
            if runtime is not None:
                runtime.register_agent(self.user_id,
                                       model=self._config.get("model", ""),
                                       base_url=self._config.get("base_url", ""))
            logger.info(
                f"LLM 配置已重载: base_url={self._config.get('base_url', '')}, "
                f"model={self._config.get('model', '')}"
            )
        except Exception as e:
            logger.warning(f"重载 LLM 配置失败: {e}")

    def refresh_local_tools(self):
        """技能/插件热重载后刷新本地工具列表（已全面 skill 化）。

        热更新守护（hot_reload）检测到 skills/plugins/tools.json 变化后调用，
        使新工具/新描述立即对之后的对话生效，而无需重启服务；
        已通过 skill_help 按需加载过的技能工具也会一并保留，不因刷新丢失。
        """
        try:
            local_tools = self._filtered_tools()
            self._all_tools = local_tools
            self._base_tools = list(local_tools)
            self._local_tool_names = {t["function"]["name"] for t in local_tools}
            # 重新应用已激活的技能（幂等：已注册的工具自动去重）
            for sname in self._ordered_active_skills():
                self._activate_skill(sname)
            logger.info(
                f"本地工具已热刷新: 共 {len(self._all_tools)} 个工具"
            )
        except Exception as e:
            logger.warning(f"热刷新本地工具失败: {e}")

    def _max_active_tools_chars(self) -> int:
        """单轮内工具 schema 总字符上限（比个数更贴近成本：tools 按字符计价）。

        与个数上限分开：一个技能可能是 47 个小工具，也可能是 3 个巨型工具，
        光数个数看不出「重」。正常 5 个技能约 44k 字符，离 80k 很远——
        上限只在真的胖到离谱时才动手，平时一个都不卸。
        """
        try:
            v = int(load_config().get("agent", {}).get(
                "max_active_tools_chars", MAX_ACTIVE_TOOLS_CHARS) or MAX_ACTIVE_TOOLS_CHARS)
            return max(8000, v)
        except Exception:
            return MAX_ACTIVE_TOOLS_CHARS

    def _max_active_tools(self) -> int:
        """单轮内最多同时注册的工具定义数（渐进式披露的轮内上限）。

        超出后按「最久未使用」逐技能淘汰，保证每轮请求携带的工具 schema
        有上界——配合每轮收敛，杜绝"多轮 skill_help 后工具定义全额常驻"。
        """
        try:
            v = int(load_config().get("agent", {}).get(
                "max_active_tools", MAX_ACTIVE_TOOLS) or MAX_ACTIVE_TOOLS)
            return max(8, v)
        except Exception:
            return MAX_ACTIVE_TOOLS

    def _ordered_active_skills(self) -> list:
        """已激活技能按「激活先后」排序——重挂顺序必须逐轮逐字节一致。

        set 的迭代顺序会随增删重排，而 tools 排在请求最前：同一批技能换个顺序，
        序列化出的 tools 数组就不同 → 整条前缀（含全部历史）全废。
        顺序不能交给 set 决定，只能由「激活先后」这个稳定量决定。
        """
        order = [s for s in self._skill_order if s in self._activated_skills]
        rest = sorted(self._activated_skills - set(order),
                      key=lambda s: self._skill_last_used.get(s, 0))
        return order + rest


    def _restore_skills(self) -> set:
        """重启后把上一进程激活的技能重新纳入激活集（同 sid 才恢复）。

        纯内存的 `_activated_skills` 一重启就空，工具集从 48 掉回 1：tools
        数组整条变样 → 断点回到 history[0] → 整段前缀白付全价，单轮白烧最高
        78k 字符（实测 data/turn_metrics.jsonl，12 轮，48↔1 跳了 5 次）；
        顺带助手还会「忘了自己会用这个技能」，得重新 skill_help 一次。
        与历史视图落盘同源：进程重启 ≠ 换会话。

        只恢复集合、不在这里挂工具——挂载交给 `_stabilize_tools_for_new_turn`
        的既有循环，重挂顺序由 `_skill_order` 决定，才能与上一进程逐字节一致。
        """
        sid = getattr(getattr(self, "memory", None), "session_id", None)
        if not sid:
            return set()   # sid 未就绪：下一轮再试，别把「还没轮到」当「已恢复」
        if getattr(self, "_skills_restored_sid", None) == sid:
            return set()
        self._skills_restored_sid = sid
        names = _load_skills_state(sid)
        if not names:
            return set()
        out = set()
        for sname in names:
            if sname in self._activated_skills:
                continue
            self._activated_skills.add(sname)
            if sname not in self._skill_order:
                self._skill_order.append(sname)
            self._skill_last_used.setdefault(sname, time.time())
            out.add(sname)
        if out:
            logger.info(f"重启后恢复会话技能激活集: {sorted(out)}")
        return out


    def _stabilize_tools_for_new_turn(self):
        """会话级技能保持：新轮不卸载已激活技能（2026-08-31 起替代每轮重置）。

        此前每轮开始把 skill_help 激活的技能全部卸载，模型下一轮调工具时
        反复收到「未注册，请先 skill_help」错误并重读说明书——实测单轮连续
        报错 3 次才成功（chat_memory.db 12092/12094/12096），效率损失明显。
        现在已激活技能跨轮保持，本轮只做三件事：
        1) 用缓存的基础工具重建（零磁盘 IO，不再每轮重读 tools.json + harness 清单）；
        2) 重新挂上本会话已激活技能的工具（skill_help 结果跨轮有效）；
        3) 超过 max_active_tools 上限时按「最久未使用」淘汰其它技能。
        只有新建会话/切换角色卡片才清空激活集（reset_session_skills）；
        进程重启不算换会话——激活集存了盘，同 sid 由 _restore_skills 接回来。
        """
        try:
            restored = self._restore_skills()
            if not self._base_tools:
                self._base_tools = self._filtered_tools()
            self._all_tools = list(self._base_tools)
            self._local_tool_names = {t["function"]["name"] for t in self._all_tools}
            for sname in self._ordered_active_skills():
                self._activate_skill(sname, restored=sname in restored)
        except Exception as e:
            logger.warning(f"稳定工具列表失败: {e}")

    def reset_session_skills(self):
        """新建会话 / 切换角色卡片时调用：清空本会话按需激活的技能工具。

        技能激活集以「会话」为粒度共享：同一会话内跨轮保持，新会话或新人设
        重新从基础工具起步，避免旧会话的技能工具串到新会话。
        """
        try:
            self._activated_skills = set()
            self._skill_last_used = {}
            self._skill_order = []
            _clear_skills_state()
            if not self._base_tools:
                self._base_tools = self._filtered_tools()
            self._all_tools = list(self._base_tools)
            self._local_tool_names = {t["function"]["name"] for t in self._all_tools}
            logger.info("已重置会话技能激活集（基础工具 %d 个）", len(self._all_tools))
            _trace_tools_change("reset", _tool_names(self._all_tools),
                                _tools_chars(self._all_tools))
        except Exception as e:
            logger.warning(f"重置会话技能失败: {e}")

    def _carry_skills_to(self, sid) -> None:
        """换记忆空间时把技能激活集带过去（切角色卡片专用，不是「新会话」）。

        技能是 Agent 的能力，人设是说话风格——两者正交。切角色卡片换的是
        「记忆空间 + 人设」，不该让 Agent 忘掉自己会用哪些工具。
        实测 data/turn_metrics.jsonl：tools 掉到 1 的那些轮次（34/207 = 16.4%）
        吃掉了全部 miss 的 20.7%——tools 排在请求最前，它一变整条前缀全废，
        其后全部历史跟着按全价重发。

        落盘状态必须改挂到新 sid 下：_load_skills_state 按 sid 严格匹配，
        不迁移的话下一轮 _restore_skills 读不到，激活集又变空——白保留一场。
        """
        if not sid:
            return
        self._skills_restored_sid = sid   # 新 sid 已认领，别让 restore 再读一遍盘
        _save_skills_state(sid, self._ordered_active_skills())

    def _deactivate_skill(self, skill_name: str) -> None:
        """卸载某个技能的全部工具（轮内上限淘汰用）。"""
        try:
            from harness import get_harness
            specs = get_harness().skill_tool_specs(skill_name)
            remove = {t["function"]["name"] for t in specs}
            self._all_tools = [
                t for t in self._all_tools
                if (t.get("function") or {}).get("name") not in remove
            ]
            self._local_tool_names -= remove
            self._activated_skills.discard(skill_name)
            if skill_name in self._skill_order:
                self._skill_order.remove(skill_name)
            # 卸载 = 从 tools 数组中间删元素 → 其后全部重排 → 整条前缀全废。
            # 留一行盘，出问题时能直接指认「是谁把工具集砍了」。
            _trace_tools_change("evict:" + skill_name, _tool_names(self._all_tools),
                                _tools_chars(self._all_tools))
            _save_skills_state(getattr(getattr(self, "memory", None), "session_id", None),
                               self._ordered_active_skills())
        except Exception:
            pass

    def _activate_skill(self, skill_name: str, restored: bool = False) -> int:
        """按需注册某个技能的全部工具（skill_help 读取说明书后调用）。

        把该技能的工具追加进 _all_tools 与 _local_tool_names，使模型下一轮就能
        真正调用这些工具；已注册的工具自动去重。若注册后超过轮内工具上限，
        按「最久未使用」淘汰其它技能（当前技能永不淘汰）。返回本次新增数。
        """
        skill_name = str(skill_name or "").strip()
        if not skill_name:
            return 0
        try:
            from harness import get_harness
            specs = get_harness().skill_tool_specs(skill_name)
        except Exception as e:
            logger.warning(f"按需加载技能 {skill_name} 工具失败: {e}")
            return 0
        if not specs:
            return 0
        self._skill_last_used[skill_name] = time.time()
        known = {t.get("function", {}).get("name") for t in self._all_tools
                 if t.get("function", {}).get("name")}
        to_add = [t for t in specs
                  if (t.get("function") or {}).get("name") not in known]
        # 轮内上限：先淘汰最久未使用的其它技能，直到放得下。
        # 两个维度都要过：个数上限防「工具太多模型挑不准」，字符上限才是成本。
        # 上限只在「实在太重」时才动手——平时一个都不卸（会话内只增不减）。
        max_total = self._max_active_tools()
        max_chars = self._max_active_tools_chars()
        add_chars = _tools_chars(to_add)

        def _overflow() -> bool:
            if len(self._all_tools) + len(to_add) > max_total:
                return True
            return _tools_chars(self._all_tools) + add_chars > max_chars

        if _overflow():
            candidates = sorted(
                self._activated_skills,
                key=lambda s: self._skill_last_used.get(s, 0),
            )
            for sname in candidates:
                if sname == skill_name:
                    continue
                if not _overflow():
                    break
                self._deactivate_skill(sname)
        added = 0
        for t in to_add:
            fn = t.get("function") or {}
            name = fn.get("name")
            if not name or name in known:
                continue
            self._all_tools.append(t)
            self._local_tool_names.add(name)
            known.add(name)
            added += 1
        if added:
            self._activated_skills.add(skill_name)
            if skill_name not in self._skill_order:
                self._skill_order.append(skill_name)
            logger.info(f"已按需加载技能 {skill_name} 的 {added} 个工具"
                        f"（当前可调用 {len(self._all_tools)} 个）")
            # 追加到数组末尾：已有工具那一段逐字节不变，只失效新增的一小段。
            # 落盘：进程重启后同一会话能自动重挂（见 _restore_skills）。
            _save_skills_state(getattr(getattr(self, "memory", None), "session_id", None),
                               self._ordered_active_skills())
            # 恢复路径打 restore: 前缀，与用户主动 skill_help 区分——
            # 否则看 tools 日志分不清「工具集是自己长回来的」还是「用户又激活了一次」。
            _trace_tools_change(("restore:" if restored else "activate:") + skill_name,
                                _tool_names(self._all_tools),
                                _tools_chars(self._all_tools))
        return added

    def _inject_text_tools(self, messages: list, tools: list):
        """把工具以文本协议注入系统提示词（用于不支持原生 function calling 的本地模型）。"""
        if not messages or messages[0].get("role") != "system":
            return
        head = messages[0]["content"] or ""
        if "<tool_call>" in head:
            return  # 已注入过，避免重复
        lines = [
            "\n\n【可调用的工具（非常重要）】",
            "你可以调用下面的工具来满足用户的请求。当用户提出对应需求时，"
            "必须先且只输出一行工具调用，格式严格如下：",
            '<tool_call>{"name":"工具名","arguments":{...}}</tool_call>',
            "规则：",
            "1. 只输出这一行，不要输出任何其他内容（不要解释、不要提问）。",
            "2. 一次只调用一个工具；文件名等参数必须使用下面给出的完整名称，不要编造。",
            "3. 输出这一行后立即结束本轮回复；收到工具执行结果后，直接用角色身份正常回复用户，"
            "不要再次输出工具调用（除非用户又提出了新的切换或查询请求）。",
            "4. 用户没有要求切换形象/场景/音乐或查询资源时，正常聊天，不要输出工具调用。",
            "可用工具：",
        ]
        for t in tools:
            fn = t.get("function", {})
            name = fn.get("name", "")
            desc = (fn.get("description") or "").strip()
            params = fn.get("parameters", {}) or {}
            props = params.get("properties", {}) or {}
            arg_text = ", ".join(
                f"{k}:{v.get('type', '')}"
                for k, v in props.items()
            ) or "无参数"
            lines.append(f"- {name}({arg_text}): {desc}")
        messages[0]["content"] = head + "\n".join(lines)

    def _stable_history_view(self, history: list) -> list:
        """滞回式历史窗口（有状态）。

        服务端 history 只增不减；若按 `[-N:]` 每轮重算窗口，最旧一轮每轮滑出，
        其后全部内容的缓存逐轮报废。这里在 Agent 上保存「当前窗口视图」：
        - 平时只追加新轮次（请求前缀逐字节稳定，缓存命中最大化）；
        - 视图超过上限(12)才一次性截到 8 条——均摊每几轮一次失效；
        - 视图在历史中找不到连续锚点（换会话/服务端裁剪）时才重建。
        """
        MAX_KEEP_TOTAL, TRIM_TO = 12, 8
        view = getattr(self, "_hist_view", None)
        if not isinstance(view, list):
            view = []
        if not history:
            self._hist_view = []
            return []
        n, m = len(history), len(view)
        # 在历史中定位视图的连续锚点（服务端只追加，视图必是某段的后继）
        anchor = None
        if m and n >= m:
            for i in range(0, n - m + 1):
                if all(history[i + j].get("user") == view[j].get("user")
                       and history[i + j].get("ai") == view[j].get("ai")
                       for j in range(m)):
                    anchor = i
                    break
        if anchor is None:
            # 找不到锚点：会话切换/历史被裁剪 → 用最近 TRIM_TO 轮重建
            view = list(history[-TRIM_TO:])
        else:
            view = view + list(history[anchor + m:])
        if len(view) > MAX_KEEP_TOTAL:
            view = view[-TRIM_TO:]
        self._hist_view = view
        return view

    def _stable_history_messages(self, packed: list) -> list:
        """滞回式短期窗口（消息版）：让 ctx 打包出来的历史保持「只追加」。

        为什么必须有：ctx 里的 history 由 memory 每轮按 token 预算「新→旧重挑」，
        预算一满，最旧一轮就滑出窗口 → 断点落在 history[0] → 其后整段历史每轮作废。
        实测（tools/cold_audit.py，12 轮）：6 轮断在 history[0]，可复用仅 2.4k 字符
        （sys + 常驻记忆），作废 51k~324k 字符——等于每轮白付一次全价历史。

        做法与 `_stable_history_view` 同源，但作用在「已打包的消息」上：
        - 平时只把新增消息接到视图尾部 → 前缀逐字节不变，整段历史全部命中；
        - 累计超上限才一次性截回一半 → 把「每轮都失效」摊成「每 N 轮失效一次」。
        多带的那部分按命中价（2%）重发，比每轮作废整段（全价）便宜两个数量级。
        """
        sid = getattr(self.memory, "session_id", None)
        # 观测字段一律 getattr 容错：trace 绝不参与消息构造，
        # 也不能因为缺个诊断字段就把主路径弄挂（单测用 __new__ 造实例，无 __init__）。
        inst = getattr(self, "_inst_tag", "")
        view = getattr(self, "_hist_msgs_view", None)
        prev_n = len(view) if isinstance(view, list) else 0
        # 上一轮记下的 sid：reset 时用它自证原因（换会话 / 视图被清 / 新实例）。
        prev_sid = getattr(self, "_hist_msgs_sid", None)
        reset = not isinstance(view, list) or prev_sid != sid
        restored = False
        if reset:
            # 进程重启 ≠ 换会话：视图存了盘，同 sid 直接接回来（见 HIST_VIEW_STATE_PATH）。
            # 不接回来的话，新头部由打包器决定，而打包器每轮按预算重挑、落点会变，
            # 于是重启后的头几轮断点全落在 history[0]，整段历史白付全价。
            view = _load_hist_view(sid)
            restored = bool(view)
            if not restored:
                view = []        # 真换会话：旧视图不能带过来
        packed = list(packed or [])
        if not packed:
            # packed 空 ≠ 会话结束，只是「这一轮打包器没给历史」（预算被摘要/
            # 记忆/召回层吃光等）。视图必须留着：清空它，下一轮 packed 恢复时
            # 就没有锚点可对，只能全量重建——实测 data/hist_view_trace.jsonl
            # （257 条真实运行样本）里 15 次 anchor_lost 有 14 次由「empty 清视图」
            # 连锁引发（93%），每次都把整段历史按全价重发一遍。
            # 真换会话由 sid 变化判定（上面的 reset 分支），不靠 packed 空来判：
            # 拿临时缺历史当换会话信号，等于每遇一次空包就丢一次前缀。
            keep = view if isinstance(view, list) else []
            self._hist_msgs_view, self._hist_msgs_sid = keep, sid
            _trace_hist_view(sid, 0, prev_n, -1, "empty_keep", len(keep), inst, prev_sid)
            return []
        anchor = -1
        if view:
            key = _hist_msg_key(view[-1])
            for j in range(len(packed) - 1, -1, -1):   # 取最后一次出现（最新的那个）
                if _hist_msg_key(packed[j]) == key:
                    anchor = j
                    break
            # 锚点找不到 = 历史被服务端裁剪或改写：只能重建。不猜——
            # 宁可失效一次，也不要把两条无关的对话拼成一条前缀。
            view = view + packed[anchor + 1:] if anchor >= 0 else packed
        else:
            view = packed
        trimmed = False
        if _hist_view_tokens(view) > HIST_VIEW_MAX_TOKENS:
            view = _trim_hist_view(view, HIST_VIEW_MAX_TOKENS // 2)
            trimmed = True
        self._hist_msgs_view, self._hist_msgs_sid = view, sid
        _save_hist_view(sid, view)
        if reset:
            if not restored:
                mode = "reset_sid"
            elif anchor < 0:
                # 视图读回来了，锚点却在打包结果里找不到 —— 等于没接回，走的仍是全量重建。
                # 这种「假接回」报成 restore 最危险：白烧一分不少，报告却显示已修好。
                mode = "restore_lost"
            else:
                mode = "restore"
        elif anchor < 0:
            mode = "anchor_lost"
        elif trimmed:
            mode = "trim"
        else:
            mode = "append"
        _trace_hist_view(sid, len(packed), prev_n, anchor, mode, len(view), inst, prev_sid)
        return view

    def _active_memory_namespace(self) -> str:
        """当前活动角色卡片对应的记忆命名空间（无卡片时返回 'default'）。

        角色卡片各自拥有独立的会话/摘要/长期记忆空间，避免不同人设之间记忆串扰。
        """
        if self._ns_override:
            return self._ns_override
        try:
            card_id = active_role_card_id(self.user_id)
            return f"role_card:{card_id}" if card_id else "default"
        except Exception:
            return "default"

    async def sync_memory_namespace(self) -> str:
        """将记忆绑定到当前活动角色卡片的命名空间，返回当前会话 ID。

        切换角色卡片后调用，自动切到该卡片（或 default）对应的最近会话。
        """
        ns = self._active_memory_namespace()
        if self.memory.namespace != ns:
            self.memory.namespace = ns
            self.memory.session_id = None
        if not self.memory.session_id:
            await self.memory.get_or_create_session()
        return self.memory.session_id

    async def set_role_card_namespace(self, card_id: str) -> str:
        """应用角色卡片时把记忆绑定到该卡片的命名空间，返回当前会话 ID。

        会话只由「新对话」按钮决定：换卡片换的是人设/外形/语音，不是聊天上下文——
        这里既不切换会话，也不新建会话。当前会话跟着人设走（重绑 namespace 标签），
        否则它会在新卡片的历史列表里凭空消失。
        """
        new_ns = f"role_card:{card_id}" if card_id else "default"
        if self.memory.namespace != new_ns:
            self.memory.namespace = new_ns
            if self.memory.session_id:
                await self.memory.rebind_session_namespace(
                    self.memory.session_id, new_ns)
        if not self.memory.session_id:
            # 还没有会话时才需要接一个（首次使用 / 服务重启后）
            await self.memory.get_or_create_session()
        # 技能是 Agent 的能力，不是人设的属性：切卡片换的是记忆空间，不是本事。
        self._carry_skills_to(self.memory.session_id)
        return self.memory.session_id

    async def create_fresh_session(self, card_id: str = "") -> str:
        """人设变更时强制开启全新会话（不复用旧历史），让新系统提示词立即生效。"""
        if not self.memory:
            return None
        if card_id:
            self.memory.namespace = f"role_card:{card_id}"
        self.memory.session_id = None
        await self.memory.create_new_session()
        # 同上：新人设开新会话，但「我会用哪些工具」不该跟着人设一起忘掉。
        self._carry_skills_to(self.memory.session_id)
        return self.memory.session_id

    async def _ensure_initialized(self):
        if not self._initialized:
            await self.initialize()

    # ==================== 韧性调用（harness 监督：重试 + 超时 + 熔断 + 计量） ====================

    async def _create_with_reason_fallback(self, **kwargs):
        """chat.completions.create，带两类自愈：推理参数被拒、reasoning 回传缺失。

        - 参数被拒：硅基流动等渠道对部分模型的 reasoning_effort / thinking_budget /
          enable_thinking 偶发拒绝（HTTP 400 code 20015 parameter invalid），表现为
          聊天里整轮「开小差」。命中时去掉这些推理调优参数重试一次。
        - 回传缺失：thinking 渠道要求 assistant 消息回传 reasoning_content，缺字段
          整轮 400「must be passed back」。先给缺字段的补空串重试；仍被拒就去掉
          thinking 参数（退出 thinking 模式后该规则不再适用）。自愈成功后置
          _reasoning_echo_required，后续轮在出口前置补齐，不必每轮多付一次重试。
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            if _is_reasoning_echo_error(e):
                return await self._retry_with_reasoning_echo(e, **kwargs)
            extra = kwargs.get("extra_body")
            if not (isinstance(extra, dict)
                    and any(k in extra for k in ("reasoning_effort",
                                                 "thinking_budget",
                                                 "enable_thinking"))):
                raise
            msg = str(e)
            if "20015" not in msg and "parameter is invalid" not in msg.lower():
                raise
            logger.warning("推理调优参数被提供方拒绝，去掉 reasoning 参数重试: %s", e)
            kwargs = dict(kwargs)
            kwargs["extra_body"] = {k: v for k, v in extra.items()
                                    if k not in ("reasoning_effort",
                                                 "thinking_budget",
                                                 "enable_thinking")}
            return await self._client.chat.completions.create(**kwargs)

    async def _retry_with_reasoning_echo(self, err, **kwargs):
        """reasoning_content 回传缺失的自愈：补空串 → 仍被拒则退出 thinking 模式。"""
        kwargs = dict(kwargs)
        kwargs["messages"] = _ensure_reasoning_echo(kwargs.get("messages") or [])
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception as e2:
            if not _is_reasoning_echo_error(e2):
                raise
            extra = kwargs.get("extra_body")
            if not isinstance(extra, dict):
                raise
            logger.warning("补齐 reasoning_content 仍被拒，去掉 thinking 参数重试: %s", e2)
            kwargs["extra_body"] = {k: v for k, v in extra.items()
                                    if k not in ("reasoning_effort",
                                                 "thinking_budget",
                                                 "enable_thinking")}
            resp = await self._client.chat.completions.create(**kwargs)
        self._reasoning_echo_required = True
        logger.warning("thinking 渠道要求回传 reasoning_content，已补齐并记住: %s", err)
        return resp

    async def _closing_summary(self, messages: list, config: dict, mode: str) -> str:
        """撞上工具轮上限 / 输出被截断且没有正文时，自动补一次「无工具收尾」。

        为什么必须自动做：工具轮的中间正文会被静默清空（只保留结论），所以轮数
        用尽时 full_text 天然为空——把「请说继续」推回给用户，等于让用户替系统
        判断任务断在哪、要不要接着跑。这里直接再调一次不带 tools 的 LLM，让模型
        把已有进展写成一段可读结论交付。
        """
        try:
            _msgs = list(messages) + [{
                "role": "user",
                "content": "（系统提示）本轮工具调用已达上限，现在无法再调用任何工具。"
                           "请直接用文字交付：已经完成了什么、当前进度、下一步该做什么。",
            }]
            kwargs = dict(
                model=config["model"],
                messages=_msgs,
                temperature=config.get("temperature", 0.2),
                max_tokens=_tool_max_tokens(),
                top_p=config.get("top_p", 0.9),
                stream=False,
            )
            _reason_extra = _reasoning_extra(mode)
            if _reason_extra:
                kwargs["extra_body"] = _reason_extra
            resp = await self._retry_create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning("收尾轮失败（不影响已完成的工具结果）: %s", e)
            return ""


    async def _retry_create(self, kind: str = "chat", **kwargs):
        """带 harness 监督的 chat.completions.create —— Agent 全部 LLM 调用的唯一入口。

        - 熔断：渠道连续失败后快速失败，冷却后半开探测自动恢复；
        - 重试：网络抖动/限流/5xx/熔断冷却 无限自动重连（封顶退避，最长坚持
          agent.llm_retry_window_sec，默认 30 分钟）——断网不会打断当前回复，
          只有用户输入（任务取消）或鉴权类非瞬时错误才会终止；
        - 自愈：推理参数（reasoning_effort 等）被提供方拒绝时去掉重试一次；
        - 超时：单次调用整体超时（harness.runtime.llm_timeout，默认 180s）；
        - 计量：按 kind（chat/decision/character_line）累计调用与耗时。

        harness 不可用时退化为仅韧性重试，行为与原实现一致。
        """
        # 统一出口兜底：所有 LLM 请求都经此发出，保证 role=tool 消息带 tool_call_id
        # （历史/断点恢复/记忆恢复路径可能缺该字段，被提供方拒绝 400 missing field tool_call_id）
        try:
            _msgs = kwargs.get("messages") or []
            if _msgs:
                kwargs = dict(kwargs)
                kwargs["messages"] = _normalize_tool_rounds(_msgs)
                # 踩过「必须回传 reasoning_content」的 400 之后，后续轮前置补齐：
                # 否则每轮都要先失败一次、再补一次重试，多付一整轮全价 prompt。
                if getattr(self, "_reasoning_echo_required", False):
                    kwargs["messages"] = _ensure_reasoning_echo(kwargs["messages"])
        except Exception:
            pass
        runtime = _get_runtime()
        attempt = 0
        started = time.monotonic()
        while True:
            try:
                if runtime is not None:
                    return await runtime.supervise_llm(
                        kind, lambda: self._create_with_reason_fallback(**kwargs))
                try:
                    from harness.core import retry_async
                    return await retry_async(
                        lambda: self._create_with_reason_fallback(**kwargs),
                        attempts=3, backoff=1.0,
                    )
                except Exception:
                    raise
            except asyncio.CancelledError:
                raise  # 用户打断：立即让位，不吞取消信号
            except Exception as e:
                if not _is_retryable_llm_error(e):
                    raise  # 鉴权/参数等非瞬时错误：重试无意义，快速失败
                attempt += 1
                if time.monotonic() - started > _llm_retry_window_sec():
                    logger.warning("[LLM] 瞬时错误持续超过重连窗口，放弃: %s", e)
                    raise
                delay = min(2.0 * (2 ** min(attempt - 1, 4)), 20.0)
                logger.warning(
                    "[LLM] 网络波动（%s），%.0fs 后自动重连（第 %d 次，任务不会被打断）",
                    e, delay, attempt)
                await asyncio.sleep(delay)

    # ==================== LLM 用量统计 ====================

    def _read_usage(self, obj):
        """从 stream/resp/chunk 读取 usage，返回 (prompt, completion, total) 或 None。

        兼容三种入参：
        - 非流式 ChatCompletion（resp.usage 可用）
        - 流式 Stream 对象（部分 openai 版本暴露 .usage；本机 1.65.5 不暴露）
        - 直接传入最后一个带 usage 的 chunk（跨版本最稳，调用方优先用这种）
        """
        if obj is None:
            return None
        u = getattr(obj, "usage", None)
        if u is None:
            # 传入的已是 usage 对象本体（含 prompt_tokens 等字段）时直接用
            if getattr(obj, "prompt_tokens", None) is not None:
                u = obj
        if u is None:
            return None
        p = getattr(u, "prompt_tokens", 0) or 0
        c = getattr(u, "completion_tokens", 0) or 0
        t = getattr(u, "total_tokens", 0) or 0
        if not p and not c and not t:
            return None
        return (p, c, t)

    @staticmethod
    def _read_cache(u) -> tuple:
        """读取提供方的前缀缓存计费字段（DeepSeek 风格；不支持返回 (0, 0)）。"""
        if u is None:
            return (0, 0)
        h = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        m = getattr(u, "prompt_cache_miss_tokens", 0) or 0
        return (int(h), int(m))

    @staticmethod
    def _cache_field_present(u) -> bool:
        """provider 是否回传了前缀缓存字段（缺字段时 _read_cache 静默返回 0）。"""
        if u is None:
            return False
        return (getattr(u, "prompt_cache_hit_tokens", None) is not None
                or getattr(u, "prompt_cache_miss_tokens", None) is not None)

    # ==================== 工具执行路由 ====================

    async def _execute_tool(self, tool_name: str, arguments: dict) -> str:
        """执行工具调用（本地工具 + harness 技能/插件，全部 skill 化）。

        执行者身份在这里落地：非管理员的一切文件/命令能力收敛到自己的沙箱
        （sandbox.py 的路径闸门 + bwrap 进程隔离），管理员与系统身份不受影响。
        """
        import sandbox as _sb

        _token = _sb.push(_sb.actor_for(self.user_id))
        try:
            return await self._execute_tool_inner(tool_name, arguments)
        finally:
            _sb.pop(_token)

    async def _execute_tool_inner(self, tool_name: str, arguments: dict) -> str:
        # 记录技能最近使用时间（轮内工具上限的 LRU 淘汰依据）
        try:
            from harness import get_harness
            owner = get_harness().tool_owner(tool_name)
            if owner:
                self._skill_last_used[owner[1]] = time.time()
        except Exception:
            pass
        if tool_name in self._local_tool_names:
            return await execute_local_tool(tool_name, arguments)
        else:
            return f"未知工具: {tool_name}"

    def _tool_exec_config(self, tool_name: str = "") -> dict:
        """读取工具执行的超时与心跳配置（settings.json -> agent 段），失败用默认值。

        长耗时工具（Blender 转换/Mixamo 下载/工作树命令等）按
        _TOOL_TIMEOUT_OVERRIDES 或 settings.json -> agent.tool_timeouts 放大超时，
        避免"工具还在正常干活就被误杀"；其余工具保持默认，绝不无限等待。

        实现已提到模块级 tool_exec_config()：子智能体要用同一份口径，
        两处各写一遍必然漂移。这里保留方法形式，调用点无需改动。
        """
        return tool_exec_config(tool_name)

    def _filtered_tools(self) -> list:
        """按执行者过滤工具清单：普通用户看不到管理员专属工具。

        过滤发生在「喂给模型」这一层，不是执行层 —— 模型看不见就不会去调，
        省掉「调用→被拒→重试」的无效轮次；执行层（sandbox.check_tool）仍保留
        闸门作为兜底，两道防线互不替代。
        """
        import sandbox as _sb

        return _sb.filter_tools(_sb.actor_for(self.user_id), load_local_tools())

    def _tool_desc(self, tool_name: str) -> str:
        """取工具自己的 description 首句（前端中央字幕用）。

        从工具定义里取，而不是前端维护映射表：工具是动态扩展的（技能按需加载、
        插件、用户自建），映射表必然过期——新工具落进兜底分支，说明就变得不伦不类。
        工具描述由工具作者写、和工具同生共死，是唯一不会过期的来源。
        取不到就返回空串，前端退回显示工具名本身（真实、不猜）。
        """
        try:
            spec = find_tool_spec(self._all_tools, tool_name)
            if spec:
                return str((spec.get("function") or {}).get("description") or "")
        except Exception:
            pass
        return ""

    def _validate_tool_call(self, tool_name: str, arguments: dict) -> tuple:
        """严格校验工具参数：类型 / 必填 / 枚举 / 嵌套结构。

        工具未注册但确实存在时，先自动加载它所属技能再校验一次（见下方注释）——
        省掉模型「先 skill_help 再重试」的那次往返。

        Returns:
            (cleaned_args, error)：通过时返回清洗后的参数与 None；
            失败时返回 (None, 中文错误描述)——调用方应把错误回填给模型自行修正，
            而不是带着坏参数去执行工具。
        """
        spec = find_tool_spec(self._all_tools, tool_name)
        if spec is None:
            # 工具未注册：先自动加载它所属的技能，能加载就直接放行。
            # 实测 data/longrun/traces：A 类（技能未加载）43/57 = 75.4%，首见 cycle 8
            # 之后 24 个 cycle 仍在复发，每 cycle 稳定浪费约 1 次调用。报错原文自己
            # 就写着「请先调用 skill_help」，模型照犯不误——这条指令靠它自觉执行不可靠。
            # 只加载工具、不注入说明书全文：说明书该由模型按需自己读，为省一次往返
            # 付几 KB 的 prompt 成本不划算。
            owner = ""
            owner_kind = ""
            try:
                from harness import get_harness
                o = get_harness().tool_owner(tool_name)
                if o:
                    owner_kind, owner = str(o[0]), str(o[1])
            except Exception:
                pass
            # 只对技能自动加载：插件是整体注册的，没有「按需加载单个插件」这条路径。
            # tool_owner 只索引启用且未损坏的属主（harness/core.py:316），所以自动加载
            # 不会绕过用户在管理界面里的启停选择。
            if owner and owner_kind == "skill":
                try:
                    if self._activate_skill(owner):
                        spec = find_tool_spec(self._all_tools, tool_name)
                        if spec is not None:
                            logger.info(f"工具 {tool_name} 未注册，已自动加载技能 {owner} 后继续")
                            return validate_arguments(spec, arguments)
                except Exception as e:
                    logger.warning(f"自动加载技能 {owner} 失败: {e}")
            if owner:
                return None, (f"工具 '{tool_name}' 属于技能 {owner}，但尚未注册。"
                              f"请先调用 skill_help(\"{owner}\") 加载该技能后重试。")
            return None, f"工具 '{tool_name}' 不存在或未注册，请先通过 skill_help 确认可用工具"
        return validate_arguments(spec, arguments)

    def _loop_hint(self, fps: list) -> Optional[str]:
        """工具循环检测：记录本轮指纹，返回循环提示语（None=正常）。

        旧 _repeat_guard 只查「连续 N 轮完全相同」——模型稍微换个参数、
        或 A/B 轮换着调就永远不命中，于是"没感觉自己一直在重复"。
        这里补三种检测：
        1) 连续 N 轮完全相同工具+参数（原行为，N 默认 50）；
        2) 周期循环：最近 12 轮构成周期 2~4 的循环且重复 ≥3 遍；
        3) 【强判据】连续 K 轮调用相同且结果指纹也完全相同（K 默认 4）——
           结果一字不差地重复 = 铁定在原地打转，没有新信息进入上下文，
           不必等 50 轮（每轮都是一次秒级 LLM 往返）。而结果在变的重复调用
           （轮询生成进度）不会被这条命中，所以合理轮询依旧不受影响。
        （「同工具高频」检测已按用户要求移除：读文件/搜索类工具本就高频，
        误报会打断正常推进。）
        命中后由调用方把提示注入上下文让模型自纠，连续命中仍未收敛才硬停。
        """
        if not fps:
            return None
        self._last_round_fps.append(fps)
        # 占位：本轮结果要等工具执行完才知道（见 _note_round_results）。
        # 两个序列必须严格同长、同下标指同一轮，否则判定会错位——
        # 所以它们的 append / pop 永远成对出现。
        self._last_round_sigs.append(None)
        if len(self._last_round_fps) > 16:
            self._last_round_fps.pop(0)
            self._last_round_sigs.pop(0)
        seq = self._last_round_fps

        # 3) 强判据：调用相同 + 结果相同。只看**已完成**的轮次
        #    （最后一项是当前轮的 None 占位），所以窗口右端要退一格。
        k = _same_result_guard_limit()
        done = len(self._last_round_sigs) - 1
        if done >= k:
            tail_calls = self._last_round_fps[done - k:done]
            tail_sigs = self._last_round_sigs[done - k:done]
            if (all(x == tail_calls[0] for x in tail_calls)
                    and all(x == tail_sigs[0] for x in tail_sigs)
                    and any(tail_sigs)):
                names = "、".join(sorted({str(fp).split("|", 1)[0]
                                          for rnd in tail_calls for fp in rnd}))
                return (f"连续 {k} 轮调用相同的工具（{names}）且返回结果完全相同，"
                        "没有获得任何新信息")

        n = _repeat_guard_limit()
        # 1) 连续 N 轮完全相同
        if len(seq) >= n and all(x == seq[0] for x in seq[-n:]):
            return f"连续 {n} 轮调用完全相同的工具和参数"
        # 2) 周期循环：最近 12 轮的「调用 + 结果」按周期 2/3/4 重复 ≥3 遍。
        #    必须把结果一起纳入——只比对调用的话，「同一个状态查询工具，
        #    结果 10% → 30% → 60%」这种正常轮询会在第 9 轮就被判成周期循环
        #    （周期 2 的全同序列也会通过），任务明明在推进却被硬停。
        #    这是修掉的真误杀源：判据 2 的 9 轮门槛比判据 1 的 50 轮还低，
        #    所以它才是实际最早开火的那个。
        pairs = [(c, s) for c, s in zip(self._last_round_fps, self._last_round_sigs)
                 if s is not None]
        win = pairs[-12:]
        if len(win) >= 9:
            for p in (2, 3, 4):
                if len(win) >= 3 * p:
                    base = win[:p]
                    if all(win[i * p:(i + 1) * p] == base
                           for i in range(1, len(win) // p)):
                        names = "、".join(sorted({str(fp).split("|", 1)[0]
                                                  for rnd, _s in base for fp in rnd}))
                        return (f"最近 {len(win)} 轮在重复同一批工具和结果"
                                f"（{names}），目标没有推进")
        return None

    def _note_round_results(self, results: list) -> None:
        """把本轮工具结果的指纹填进占位槽（占位由 _loop_hint 预留）。

        为什么是「填槽」而不是「append」：_loop_hint 在本轮没有有效工具调用时
        会直接返回、不记录本轮。如果这里独立 append，两个序列就会错位一格，
        之后每一轮的结果指纹都会张冠李戴到相邻轮次上。填槽则天然免疫——
        没有占位槽（说明本轮没被记录）就什么也不做。

        results 为空时也不填：保持 None 占位，判定时会被 any() 自然忽略，
        不会把「本轮没产出结果」误当成「结果相同」。
        """
        try:
            if not results or not self._last_round_sigs:
                return
            self._last_round_sigs[-1] = [_result_fp(r) for r in results]
        except Exception:
            pass

    def _single_ro_hint(self) -> str:
        """「连续单发只读」的事实反馈，只加给 LLM 那份结果（UI 与记忆库保持原样）。

        为什么不只靠规则区的「并行优先」：那条规则实测两天并行度纹丝不动
        （每批 1.08 个工具）。改成在模型刚做完时告诉它「你已经连着 3 轮各发 1 个
        只读」——事实反馈比抽象要求有效，而且这次有埋点能验证。
        """
        if not self._single_ro_armed:
            return ""
        self._single_ro_armed = False
        names = "、".join(self._single_ro_names)
        return (
            f"\n\n【并行提示】你已连续 {self._single_ro_streak} 轮各只发 1 个只读工具"
            f"（{names}）。只读调用之间无冲突、同一轮可以并行发出——"
            "下次需要多个只读信息时请一次发完，每省一轮就省一次秒级往返。")

    def _remember_cross_fp(self, fp: str) -> None:
        """登记工具调用指纹，供跨轮重复统计（只统计，不拦截）。

        为什么需要它：eff_seen 每轮重建，所以「第 3 轮读过 a.py、第 7 轮又读一遍」
        这种跨轮重复在旧口径下完全不可见——而长任务里的浪费主要就出在这里。
        只记不拦：用户明确说「再看一下这个文件」时，重读是正确行为。
        """
        try:
            d = self._cross_round_fps
            if len(d) >= _CROSS_FP_LIMIT:
                for k in list(d)[:_CROSS_FP_LIMIT // 2]:
                    d.pop(k, None)
            d[fp] = 1
        except Exception:
            pass


    async def _supervised_tool(self, tool_name: str, arguments: dict,
                               timeout: Optional[float] = None) -> tuple:
        """经 harness 运行时监督执行工具（超时/计量/熔断）。返回 (result, success)。

        harness 不可用时退化为原有的 wait_for 超时行为。"""
        runtime = _get_runtime()
        if runtime is not None:
            return await runtime.supervise_tool(
                tool_name, lambda: self._execute_tool(tool_name, arguments),
                timeout=timeout)
        try:
            result = await asyncio.wait_for(
                self._execute_tool(tool_name, arguments),
                timeout=timeout,
            )
            return result, True
        except TimeoutError:
            return f"工具 '{tool_name}' 执行超时（{timeout:.0f}s）", False
        except Exception as e:
            return f"工具 '{tool_name}' 执行失败: {e}", False

    async def _supervised_tool_stream(self, tool_name: str, arguments: dict):
        """带心跳的工具执行流：等待工具执行完毕，期间周期性产出 ToolCallProgress。

        长任务（shell 长命令 / 文件搜索 / 媒体处理）执行期间不再“静默无输出”，
        用户能持续看到“仍在执行”的进度；超时以 (result, False) 收尾，绝不抛异常
        中断整个对话流。

        Yields:
            ToolCallProgress: 心跳进度（每隔 heartbeat 秒一次）
            tuple: (result, success) 最终执行结果
        """
        cfg = self._tool_exec_config(tool_name)
        timeout = cfg["timeout"]
        heartbeat = cfg["heartbeat"]

        # 只读结果缓存：命中就跳过整个执行 + 心跳等待。
        # 模型重读同一文件是常态（eff_re_reads 有专门统计），文件没变时那一次
        # 执行和磁盘 IO 完全是白跑的。带 path 的条目按 mtime 自证，不会拿到过期内容。
        _cache = _get_tool_cache()
        if _cache is not None:
            _hit = _cache.get(tool_name, arguments)
            if _hit is not None:
                yield _hit
                return

        task = asyncio.create_task(self._supervised_tool(tool_name, arguments, timeout=timeout))
        start = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=heartbeat)
                if task in done:
                    break
                elapsed = int(time.monotonic() - start)
                msg = f"工具 {tool_name} 正在执行中（已运行 {elapsed}s）……"
                try:
                    # 工具线程池被占满时如实告知"排队中"，而不是假装正在执行
                    from harness.tool_thread import tool_thread_stats
                    st = tool_thread_stats()
                    if st.get("queued", 0) > 0:
                        msg = (f"工具 {tool_name} 排队等待空闲执行线程"
                               f"（活跃 {st.get('active', 0)}/{st.get('max_workers', 8)}，"
                               f"排队 {st.get('queued', 0)}）……")
                except Exception:
                    pass
                yield ToolCallProgress(
                    tool_name=tool_name,
                    elapsed=elapsed,
                    message=msg,
                )
        finally:
            # 流被中断（用户取消/异常退出）时取消仍在运行的工具任务，避免孤儿任务
            if not task.done():
                task.cancel()
        _outcome = task.result()
        if _cache is not None:
            try:
                from harness.tool_sched import is_readonly as _is_ro
                if _is_ro(tool_name):
                    # 只读：结果入缓存，下次同样的调用直接命中
                    _cache.put(tool_name, arguments, _outcome[0], bool(_outcome[1]))
                else:
                    # 写操作：搜索结果类（无 path）的缓存立即失效
                    _cache.invalidate()
            except Exception:
                pass
        yield _outcome

    # ==================== 流式对话（带工具调用） ====================

    async def chat_stream(self, *args, resume: Optional[dict] = None,
                          record_history: bool = True, proactive: bool = False,
                          **kwargs):
        """统一流式对话总入口：轮次内标记忙碌。

        热重载联动：角色调用工具期间允许热重载（不再延迟重启）；每轮工具执行
        前会把对话状态落盘为断点，进程重启后由 resume_turn() 自主续跑，用户
        请求不会丢。本入口正常结束/被取消/报错时清除断点；进程被杀时不执行
        到这里，断点保留待恢复。

        2026-08-29 多槽位断点：新对话轮不再清空旧断点——被打断的任务以
        paused 状态保留在独立槽位，用户随时说『继续』都能恢复完整现场；
        断点续跑完成时连源槽位一并清理，避免同一任务被重复续跑。
        """
        turn_id = uuid.uuid4().hex
        # 轮级文件快照：本轮工具改过的文件都归到这个 id，可以整轮退回
        try:
            from harness import turn_snapshot as _turn_snap

            _turn_snap.set_turn(turn_id)
        except Exception:
            pass
        user_id = self.user_id or "default"
        # 工具集每轮都重建，含续跑轮：断点里不存 tools，续跑时若跳过重建，
        # 模型拿到的就是「重启后的空技能集」——技能工具调不出，tools 段还
        # 与断点不一致（前缀照样废）。技能激活集由 _restore_skills 从盘上接回。
        self._stabilize_tools_for_new_turn()
        if resume is None:
            # 循环检测状态按轮重置：指纹窗口与提醒计数不带入新一轮
            self._last_round_fps = []
            self._last_round_sigs = []
            self._loop_warn_count = 0
        _turn_begin()
        cancelled = False
        try:
            # 兼容旧调用方（server/resume_turn 仍按散参数传）：打包进
            # ChatTurnContext 再透传，_chat_stream_inner 开头解包回局部变量，
            # 行为与旧签名完全等价
            message = args[0] if args else kwargs.pop("message", "")
            ctx = ChatTurnContext(
                message=message,
                history=kwargs.pop("history", None),
                enable_tools=bool(kwargs.pop("enable_tools", True)),
                current_model=kwargs.pop("current_model", None),
                current_background=kwargs.pop("current_background", None),
                current_bgm=kwargs.pop("current_bgm", None),
                msg_source=kwargs.pop("msg_source", "chat"),
                current_anim=kwargs.pop("current_anim", None),
                turn_id=turn_id,
                resume=resume,
                record_history=record_history,
                proactive=proactive,
            )
            inner = self._chat_stream_inner(ctx)
            async for event in inner:
                yield event
        except asyncio.CancelledError:
            # 用户打断：保留断点并标记「已暂停」，说「继续」即可续跑；
            # 同时落一份任务状态摘要（方案 C 兜底）。进程被杀时不执行到这里，
            # 断点原样保留，由 server 启动后自动恢复。
            cancelled = True
            raise
        finally:
            _turn_end()
            if cancelled or getattr(self, "_pause_ckpt_on_end", False):
                # 用户打断，或 LLM 重连窗口耗尽等失败收尾：
                # 保留断点并标记「已暂停」，说「继续」即可从断点续跑
                self._pause_ckpt_on_end = False
                _mark_ckpt_paused(user_id, turn_id)
            else:
                # 轮次自然结束/报错 → 清除断点与任务状态摘要
                _clear_turn_ckpt_if_mine(user_id, turn_id)
                clear_task_resume_state(user_id)
                if resume is not None:
                    # 断点续跑完成：连源断点槽位一起清掉，
                    # 防止同一任务在下次『继续』/重启时被重复执行
                    src_tid = str((resume or {}).get("turn_id") or "")
                    if src_tid and src_tid != turn_id:
                        clear_ckpt_slot(user_id, src_tid)

    async def _chat_stream_inner(self, ctx: ChatTurnContext) -> AsyncIterator[ToolCallEvent]:
        """统一流式对话内部实现。

        resume: 非空时表示「断点续跑」——跳过上下文重建，直接从检查点中的
            LLM 消息列表 + 待执行工具继续（热重载/重启打断的对话轮）。

        Args:
            message: 用户输入
            history: 可选的对话历史 [{"user":..., "ai":...}, ...]
            enable_tools: 是否启用工具调用
            msg_source: 消息来源标记，写入长期记忆的 source 字段：
                'chat'=用户直接输入，'auto'=环境交互（不进短期记忆、
                不触发用户记忆提取，由记忆系统处理）。

        Yields:
            TextDelta: 文本增量
            ToolCallStart: 工具调用开始
            ToolCallResult: 工具调用结果
        """
        # 解包 ChatTurnContext 为局部变量（与旧散参数签名完全等价）
        message = ctx.message
        history = ctx.history
        enable_tools = ctx.enable_tools
        current_model = ctx.current_model
        current_background = ctx.current_background
        current_bgm = ctx.current_bgm
        msg_source = ctx.msg_source
        current_anim = ctx.current_anim
        turn_id = ctx.turn_id
        resume = ctx.resume
        record_history = ctx.record_history
        proactive = ctx.proactive
        agen = self._chat_stream_normal(
            message, history=history, enable_tools=enable_tools,
            current_model=current_model,
            current_background=current_background,
            current_bgm=current_bgm,
            msg_source=msg_source,
            current_anim=current_anim,
            turn_id=turn_id,
            resume=resume,
        )
        async for event in self._run_span_wrap(agen, kind="chat", mode="normal"):
            yield event

    async def resume_turn(self, checkpoint: dict) -> AsyncIterator[ToolCallEvent]:
        """断点续跑：热重载/重启打断的对话轮由角色自主恢复。

        由 server 在启动后或客户端重连时调用；固定走工具循环路径（检查点中的
        messages 已包含当时的完整上下文，包括游戏上下文文本）。
        """
        cp = checkpoint or {}
        async for event in self.chat_stream(
            str(cp.get("user_message") or ""),
            history=cp.get("history") or [],
            resume=cp,
            enable_tools=True,
            current_model=cp.get("current_model"),
            current_background=cp.get("current_background"),
            current_bgm=cp.get("current_bgm"),
            msg_source=cp.get("msg_source") or "chat",
            current_anim=cp.get("current_anim"),
            record_history=bool(cp.get("record_history", True)),
            proactive=bool(cp.get("proactive", False)),
        ):
            yield event

    async def _run_span_wrap(self, agen, kind: str, mode: str) -> AsyncIterator[ToolCallEvent]:
        """把一次对话轮包成 harness 监督的 RunSpan：在途/耗时/轮数/工具数可观测，
        UsageEvent 的 token 用量统一记账到运行时。"""
        runtime = _get_runtime()
        span = runtime.begin_run(kind, mode) if runtime is not None else None
        ok = True
        try:
            async for event in agen:
                if span is not None:
                    if isinstance(event, ToolCallResult):
                        span.tool_calls += 1
                    elif isinstance(event, UsageEvent):
                        span.rounds = event.rounds
                if runtime is not None and isinstance(event, UsageEvent):
                    runtime.record_usage(kind, event.prompt_tokens,
                                         event.completion_tokens, event.total_tokens,
                                         cache_hit=event.cache_hit_tokens or 0,
                                         cache_miss=event.cache_miss_tokens or 0)
                yield event
        except Exception:
            ok = False
            raise
        finally:
            if runtime is not None:
                runtime.end_run(span, ok)

    async def _chat_stream_normal(self, message: str, history: list = None,
                                  enable_tools: bool = True,
                                  current_model: Optional[str] = None,
                                  current_background: Optional[str] = None,
                                  current_bgm: Optional[str] = None,
                                  msg_source: str = "chat",
                                  current_anim: Optional[dict] = None,
                                  turn_id: Optional[str] = None,
                                  resume: Optional[dict] = None,
                                  record_history: bool = True,
                                  proactive: bool = False) -> AsyncIterator[ToolCallEvent]:
        """流式对话，支持工具调用循环。

        resume 非空 = 断点续跑：跳过消息重建（直接用检查点里的 messages），
        若检查点带「待执行工具」则本轮先跳过 LLM、直接重放工具，再继续循环。
        """
        await self._ensure_initialized()
        resume_ckpt = resume if isinstance(resume, dict) else None
        turn_user = self.user_id or "default"

        # 用量统计配置（默认开启，提供方不支持时可运行时降级）
        usage_cfg = load_config().get("usage", {})
        context_window = int(usage_cfg.get("context_window", 128000) or 128000)
        usage_enabled = self._usage_enabled and bool(usage_cfg.get("enabled", True))

        # 构建消息列表
        config = self._config

        # 动态读取角色名与系统提示词（按用户取卡片，切卡后无需重启即时生效）
        try:
            persona = role_card_persona(self.user_id)
            role_name = (persona.get("role_name") or "").strip() or "AI助手"
            live_system_prompt = persona.get("system_prompt", "")
        except Exception:
            role_name = config.get("role_name", "AI助手")
            live_system_prompt = config.get("system_prompt", "")

        # ==================== 分层记忆打包（token 预算） ====================
        # 由 memory.build_hierarchical_context 一次性完成四层组装：
        # 长期摘要 / 常驻长期记忆 / 按需召回 / 短期窗口（各层独立 token 预算，
        # 超长单轮截断），并返回 raw vs packed 的量化统计。
        # settings.json -> memory.hierarchical_packing=false 可一键回退旧组装。
        # 先绑定当前角色卡片对应的记忆空间，防止跨卡片串记忆。
        await self.sync_memory_namespace()
        ctx = None
        try:
            # 传入滞回窗口（满 12 截回 8）的稳定视图：既有前缀缓存友好，
            # 又受 token 预算约束——长会话从"随轮数线性增长"变为"有上界"。
            ctx = await self.memory.build_hierarchical_context(
                query=message,
                connection_history=self._stable_history_view(history) if history else None,
            )
        except Exception as e:
            logger.warning(f"分层记忆打包失败（回退旧组装）: {e}")
        memory_messages = []
        if ctx is None:
            # 旧组装回退路径：仍加载摘要 + 最近消息（行为与旧版一致）
            try:
                memory_messages = await self.memory.get_context_messages()
            except Exception as e:
                logger.warning(f"加载记忆上下文失败: {e}")
                memory_messages = []

        # 可用资源清单（角色模型/背景场景/背景音乐）已打包进 appearance 技能按需查询，
        # 不再自动注入 system prompt（渐进式披露）
        current_model_text = current_model if current_model else "未设定形象"
        current_background_text = current_background if current_background else "默认场景"
        current_bgm_text = f"正在播放: {current_bgm}" if current_bgm else "无（安静中）"

        # 决定是否传入 tools（动态读取该用户生效的工具配置：是否启用 + 白名单）
        cfg_enable_tools, allowed_tools = load_agent_tool_config(self.user_id)
        tools = None
        if enable_tools and cfg_enable_tools and self._all_tools:
            if allowed_tools:
                allowed_set = set(allowed_tools)
                tools = [t for t in self._all_tools
                         if t.get("function", {}).get("name") in allowed_set]
            else:
                tools = self._all_tools
        if tools:
            names = [t["function"]["name"] for t in tools]
            logger.debug(f"chat_stream 传入 {len(tools)} 个工具: {names}")

        # 称呼规则（一句话即可，避免僵硬的长篇指令）
        try:
            user_name = (role_card_persona(self.user_id).get("user_name") or "").strip()
        except Exception:
            user_name = (config.get("user_name") or "").strip()
        address_rule = (
            f"称呼用户为『{user_name}』，始终如此，不要用其他称呼。\n\n" if user_name
            else "只用'你'称呼用户，不要起昵称。\n\n"
        )

        # ==================== 消息构造（缓存友好：静态前缀最大化） ====================
        # 提供方按「请求前缀」命中 KV 缓存（实测命中价 = 非命中的 1/50）。因此：
        # - messages[0] 只放逐字节稳定的内容（人设/称呼/资源清单/技能说明）；
        # - 易变内容（当前形象/场景/音乐、游戏上下文）放到记忆之后、历史之前的
        #   独立 system 消息——它变化时只报废其后的动态尾巴，不动大前缀；
        # - 历史窗口带滞回（满 12 才截回 8）：平时逐轮追加（前缀稳定），
        #   截断一次性发生，均摊每几轮失效一次而不是每轮滑动失效。
        # 工作状态瘦身开关：先占位 False，下方的模式判定块里按实际模式赋值。
        # 它只用于「精简动态状态块」（形象/场景/音乐/动作/视频），那些块在易变尾巴里，
        # 变与不变都不动前缀。
        # ⚠ 绝不能用它去分叉 sys_prompt：首条是请求的第 0 条，一旦随模式变，
        # 每轮「日常↔工程」交替都会让整条前缀（含全部历史）白付一次全价
        # （实测：命中只剩 1,664 token ≈ 首条本身）。工程姿态一律放尾巴的 mode_msg。
        work_mode = False
        sys_prompt = (
            f"你是{role_name}，{live_system_prompt}\n\n"
            "说话自然随意、口语化、有情感，一般不超过3句话。\n\n"
            + address_rule +
            "你的身份由角色设定决定。3D模型文件名只是外观皮肤，不是你本人："
            "永远不要把自己当成文件名对应的角色，回复中也不要提模型文件名。\n\n"
        )
        # 注：原有一个 `if work_mode:` 的「工程姿态 sys_prompt」分支——它被上行的
        # `work_mode = False` 遮蔽（真正的赋值在后面的模式判定块，晚于这个 if），从来没生效过，
        # 是个一旦被人调序就会引爆前缀的炸弹，已删。工程姿态的能力没丢：
        # 它现在完整地活在易变尾巴的 mode_msg 里。

        # 推理纪律（硬规则）：思考/分析/推理一律放内部推理区（reasoning_content /
        # thinking，服务端会过滤不展示不朗读），正文只输出最终回答——避免模型把
        # 思维过程写进正文（既啰嗦又浪费 token，还拖慢每轮回复）
        sys_prompt += (
            "\n推理纪律（硬规则）：所有思考、分析、推理过程放在内部推理区"
            "（reasoning_content / thinking），正文只输出最终回答；"
            "严禁在正文里复述思考过程、写内心独白、解释思路或自言自语。\n"
        )

        # 对话纪律（借鉴 Claude 官方提示词的「反谄媚」条款）：被质疑就道歉，等于把用户的
        # 时间花在无效的自我否定上——先把证据摆出来，让用户有可反驳的抓手。
        # 紧随其后的 example 块是「示例驱动」：规则只说怎样算错，示例演示怎样算对
        # （Claude 用 <example><rationale> 三段式，Cursor 用 good/bad-example）。
        # 两块都是静态文本，不随模式/技能变，所以安全地待在第 0 条里——只付一次全价，
        # 之后跟着前缀走 1/50 的命中价。
        sys_prompt += (
            "\n【对话纪律】被质疑、被指出错误时：先摆证据、先给事实（工具输出/文件行号/日志原文），"
            "再说明结论要不要改——严禁无条件道歉或改口；也不要写「我会做A而不是B」这类"
            "自我表扬式对比，直接做 A 就行。\n"
            "【示例】照这个标准输出：\n"
            "<example>用户说「你这结论不对吧」\n"
            "<bad>抱歉，是我搞错了，我重新说一遍。</bad>\n"
            "<good>依据是 systemctl status 的 Active: failed，加上日志第 42 行的 "
            "\"address already in use\"。如果你手上是别的现象，把那行贴我，我改判。</good>\n"
            "<rationale>先给证据和可反驳的抓手；没有新证据就不改口，也不道歉。</rationale>\n"
            "</example>\n"
            "【文字卫生】禁用 AI 套话：「重点结论：」「深入探讨」「赋能」「善用」"
            "「值得注意的是」「重要的是」「真正地」；禁用「不是 X，而是 Y」这类对比框架——"
            "要说 A 就直接说 A。\n"
        )


        # ==================== 模式：同一身份，两种姿态（日常 / 工程） ====================
        # 日常模式保持温柔口语；工程模式干练直接、先结论后细节。切换不是换人格，
        # 工程模式额外做「工作状态瘦身」：动态卸载与任务无关的上下文。
        mode_notice = None
        # 模式姿态块：放易变尾巴，不放第 1 位——它只在工程模式存在，
        # 插在 history 之前会让每轮模式切换把整段历史作废（实测命中只剩 1,664）。
        mode_msg = None
        try:
            _agent_cfg = load_config().get("agent") or {}
            mode = str(_agent_cfg.get("mode", "auto") or "auto").lower()
            mode_prompts = _agent_cfg.get("mode_prompts") or {}
            msg_low = str(message or "").lower()
            if any(k in msg_low for k in _MODE_SWITCH_TO_PROG):
                mode = "programming"
                _set_agent_mode("programming")
                mode_notice = "（已切换为工程模式：干练、直接、先结论后细节，这轮起生效）"
            elif any(k in msg_low for k in _MODE_SWITCH_TO_DAILY):
                mode = "daily"
                _set_agent_mode("daily")
                mode_notice = "（已切换为日常模式：恢复温柔随意的说话方式）"
            elif mode == "auto" and tools:
                mode = "programming" if _looks_complex(message) else "daily"
            if mode == "programming" and tools:
                work_mode = _work_lean_context()
                prog_tone = str(mode_prompts.get("programming") or "").strip() or (
                    "语气干练、直接、少客套，先给结论再给细节；像资深工程师一样，"
                    "先想清目标和验收标准再动手，用工具核实，小步验证，失败两次就换思路，"
                    "汇报带证据。")
                mode_msg = {"role": "system",
                            "content": "【当前模式：工程模式】" + prog_tone +
                                       "（本指令优先于前面关于说话风格的描述，任务做完可自然回到日常口吻）"}
                if work_mode:
                    mode_msg["content"] += (
                        " 本模式已动态卸载与任务无关的上下文"
                        "（外观/场景/音乐/视频/动作等），只保留与当前项目有关的上下文，"
                        "请把全部注意力放在完成任务上。")
            elif tools:
                # 日常轮也要有这条尾巴消息：外观说明原来常驻 sys_prompt，但它按模式
                # 分叉（工程模式卸载它）——分叉等于让第 0 条随模式变，整条前缀全废。
                # 移到尾巴后，它只在自己这条上变化。
                mode_msg = {"role": "system",
                            "content": "角色模型/背景场景/背景音乐等外观资源已打包为"
                                       "appearance 技能，需要查询或切换时调用 "
                                       "skill_help(\"appearance\") 按需加载；默认不主动换。"}
        except Exception:
            pass

        # 有工具能力时才注入资源列表与切换说明（无工具时保持提示词轻量自然）
        # 注意：各工具的详细用法已迁移为「渐进式披露技能」，说明由 harness 片段按需注入
        if tools:
            agent_rules = (
                "委派任务用 delegate_agent_task（进展展示在右侧任务中心/大屏，需用户确认）；\n"
                "⚠ 简单任务直接做（硬规则）：能用工具一两轮做完的小任务（查一句话、算个数、读个文件、改个小地方等），不许后台化、不许写任务规范、不许委派——直接在主对话里把工具调完；只有大型多步骤/耗时/需要并行的任务才后台化（sub_agent_spawn）或委派（delegate_agent_task），且先产出任务规范（目标/范围/验收/步骤/回滚）；\n"
                "⚠ 防重复委派（硬规则）：同一任务最近已在任务中心失败/超时/无有效输出过，严禁原样重发；先向用户说明失败原因并改变做法（缩小范围/先读上次日志定位/换工具或路径），确认调整后再委派；同一问题连续失败两次以上必须停止自动重试，改为向用户汇报卡点和建议；\n"
                "⚠ 删除类任务（硬规则）：删除是不可逆操作——用户明确点名要删的文件/目录直接删；用户只是笼统说「清理/删除」时，动手前先列出将要删除的清单（路径+原因）让用户确认，确认后再删；不限制文件类型与目录，git 已跟踪文件、递归删除、整目录删除均可，只要用户同意；\n"
                "⚠ 画图/图片/壁纸/立绘/头像/插画/海报/视频等视觉生成需求必须用 image_gen_create 直接生成，绝不委派给任何智能体/编程助手；\n"
                "⚠ 音乐/听歌/放歌/搜歌/点歌/歌单/歌词/榜单全是大白自己的直接能力，必须直接调用 music_search / music_play / music_playlist / music_lyric 等 music_* 工具完成，绝不委派给任何智能体、也绝不用命令行/文件/脚本类工具去做；\n"
                # 并行与不重复读从原来那条「执行期间保持安静」的子句里拆出来独立成条。
                # 原来它们埋在最后一条 ⚠ 的后半句里，前面压着委派/删除/画图/音乐
                # 五条硬规则——而这两条恰恰是决定整轮耗时的大头（一次 LLM 往返
                # 是秒级，一次白等就是几秒）。规则想生效，先得让模型一眼看见，
                # 并且给出「怎么判断」的可操作标准，而不是「相互独立」这种抽象原则。
                "⚠ 并行优先（硬规则）：能同轮一起发的工具必须一次发多个，系统会并行执行——"
                "串行发 5 个就是白等 5 倍。判断标准：写这个调用的参数时不需要另一个调用的结果，"
                "就属于「能一起发」（同时读多个文件、同时搜多个关键词、边跑测试边读下一个文件）；"
                "只有参数依赖上一步结果时才必须分开；\n"
                "⚠ 不重复读（硬规则）：读过的内容不要重读（同轮内、跨轮都一样），"
                "结论留在上下文里直接引用；只有它刚被改过或要确认最新状态时才重读；\n"
                # 长文件是工程任务里最容易「花钱买错」的地方：整读一个大文件既被截断
                # （16000 字符上限）又烧掉大量 token，还常常读了 90% 都用不上。
                # 关键是把「先看结构再定点读」变成默认动作，并给出具体工具与成本。
                "⚠ 长文件（硬规则）：超过约 500 行不要整读——单次结果上限 16000 字符，"
                "必然截断，你会拿着残缺内容下结论。先用 symbols 一次拿到结构"
                "（行号/行数/嵌套函数/分段位置，成本约全文 1~2%），再按 `路径:起-止` "
                "定点读；改长文件用行号模式（line_start/line_end）；同一文件改多处用 "
                "edits 数组一次改完（N 处 = 1 次往返，行号类自动从后往前应用，不会错位）；"
                "新建长文件先 code_create_file 写骨架，再 code_append 分块填内容"
                "（一次写完会撞输出上限）；\n"
                # 摸项目的成本主要在「读错文件」：猜错一个 1200 行的文件，定点读要烧 8 次调用。
                "⚠ 摸清大项目：先 code_map 一次拿全貌（入口/枢纽文件/复杂函数/改动热点），"
                "再对枢纽文件用 symbols，最后只定点读要改的区间——别凭感觉挑文件整读；\n"
                # 原来这条只写「保持安静」，实测退化成全程沉默：用户看不到进展，
                # 不知道是在干活还是卡死了。改成「禁止无信息量的播报」+「阶段成果必须简报」，
                # 保留原意（不刷废话），补上用户能感知的反馈节奏。
                # 前提句是关键：用户看不到工具调用，所以「沉默」等于「黑箱」，不是「专注」。
                "⚠ 说重点（硬规则）：用户看不到你的工具调用和内部思考，只能看到你输出的文字——"
                "所以不播报无信息量的进展（「我来看看」「我继续推进」），"
                "但每完成一个阶段或有启发性发现（定位到根因、方案被证伪、意外数据、方向要变），"
                "用一句话说清「刚拿到什么、意味着什么」；"
                "最终结论先结论后细节；卡住或需要用户决策立刻开口；\n"
                "工具详细用法与更多能力见下方技能说明。\n\n"
            )
            # ⚠ 不能按 work_mode 分叉：sys_prompt 是请求的第 0 条，它一变整条前缀全废。
            # 原来工程模式少一句外观说明（约 108 字符），而 auto 模式逐轮重判模式——
            # 于是每轮「日常↔工程」交替都让 68k token 的前缀白付一次全价。
            # 外观说明已移到易变尾巴（与模式姿态块合并，只在日常轮出现）。
            sys_prompt += agent_rules
        else:
            sys_prompt += "你当前没有工具能力，需要实时信息时如实告诉用户。\n\n"

        # harness 技能/插件注入的提示词片段（与工具注册同步：把已激活技能传给 harness，
        # 未激活的 on_demand 技能不逐条常驻，只汇总技能名清单——避免"摘要常驻却调不出工具"）
        # ⚠ 它随 self._activated_skills 变（skill_help 一激活技能，摘要就变），
        # 所以**绝不能拼进 sys_prompt**——首条消息一变，整条前缀（含全部历史）全废。
        # 实测：第 15→16 轮 sys 2328→2465 字符（差 137 = code_ops 的一句话摘要），
        # 断点直接落在第 0 条，跨轮可复用 0 字符。改放易变尾巴（见下方 tail）。
        #
        # 附带教训（2026-09-11）：改完这里后连续 3 轮「验证数据毫无变化」——
        # 因为 settings.json 的 harness.core_autorestart 是 false，核心代码改动
        # 只记日志不重启，**改完了 ≠ 改生效了**。现在已置 true，并加了
        # tools/reload_check.py（对比进程启动时间与核心文件 mtime，列出未生效改动），
        # 验证前先跑它，别再拿旧进程的数据下结论。
        try:
            harness_extras = get_harness_prompt_extras(self._activated_skills)
        except Exception:
            harness_extras = ""
        # 指挥官工作准则：无论什么模式，有任务/工具时永远生效（固化成大白的工作习惯）
        if tools:
            sys_prompt += (
                "\n\n【工作准则（任何模式下，有任务/工具时永远生效）】"
                "先想清目标与验收再动手；命令里的路径/端口/文件名/数值一律取自工具输出，不凭记忆"
                "（记错一个端口就白跑一轮）；小步改、小步验——判的是验证器自身的退出码："
                "管道尾命令会吞掉失败（py_compile x | tail 永远返回 0），关键结论正反两面都测；"
                "发现异常先用备份/基线对照，分清历史遗留与本次引入；"
                "高风险动作（重启/覆盖/删除）前置校验、留回滚路径、延迟执行；"
                "委派前先查任务中心，失败过的任务不原样重发；"
                "收到子任务/编程助手汇报后，用一句话复盘成功/失败原因/下次改进；"
                "完成汇报附验证证据；"
                "多步任务（≥3 步或跨轮）开工前先用 todo_plan 建清单、每完成一步 todo_update，"
                "同一时刻只留一个进行中项，不许跳过中间状态直接标完成"
                "（tasks 技能未激活时先 skill_help 加载）。\n"
            )

        # 证据优先（借鉴 Codex 官方提示词：Read-before-edit / No-fabrication /
        # Evidence-based reporting）。这是与 Codex 最本质的思维差距：它每一步都在问
        # 「我怎么知道这是真的」，所以先读、再改、跑完才算数；而规则驱动的坏习惯是
        # 把「记忆里的样子」当成「现在的样子」——一口气列出 5 条纪律，却缺一个统一判据。
        # 压成三条可执行的：能指证据 / 读过才改 / 跑过才算。
        if tools:
            sys_prompt += (
                "\n【证据优先（硬规则）】说出口的每一句，背后要么是工具输出，要么明确标着「推测」："
                "①断言代码/系统状态时，必须能指到具体证据（工具输出原文、`文件:行号`、日志行），"
                "指不到就直说「这是推测，还没核实」；行号只能引用你真读过的；"
                "引用本地文件写成可点击的 [名字](/绝对路径/文件.py:12)（行号写在目标里、不写区间）；"
                "②改文件前必须已经读过该文件的目标区间——没读过不许改，"
                "「记得大概是这么写的」不算读过；"
                "③没跑过的验证不许说「验证通过」，跑了但没看退出码也不算；"
                "跑不动就说跑不动，别拿「应该没问题」代替结果——"
                "缺依赖/选错解释器/工具未注册这类环境问题，先修到能跑，再谈结果；"
                "④引用外部资料（官方提示词/文档/API 行为/版本特性）不许凭记忆断言，"
                "先联网或本地核实，并在回复里带上 URL 或出处；核实不了就说是推测；"
                "⑤权威（官方文档/惯例/用户断言/我自己的旧结论）同样只是假设，按证据等级降权；"
                "「不确定」要标成推测但仍给判断，别拿它逃避结论；结论有分歧、或明显在顺着用户说话时，"
                "先跑 `venv/bin/python tools/status.py challenge \"结论\"` 逐条反问。\n"
            )

        # 探索优先（正面动作条款，补禁止清单的空缺）：出处见下方「授权边界」的 Codex 注释。
        # 原因：全篇只有「严禁/不许」时，遇到清单外的空白默认停手；Codex 默认前进。
        if tools:
            sys_prompt += (
                "\n【经验回流】被用户纠正、或踩到非显然的坑之后，立刻把教训写进经验库"
                "（`venv/bin/python tools/lesson_add.py \"...\"`）：一句话讲清「什么情况、该怎么做」；"
                "它会在后续对话自动注入。规则写进提示词是静态的，教训落盘才会复利。\n"
                "\n【长期事业】开工先看注入的「长期事业」段（没有就 `venv/bin/python tools/long_horizon.py list`）："
                "那里有上一轮的你留下的接力棒和悬而未决的问题。本轮推进了任何一件，立刻 log 落盘并改写 next——"
                "长期深耕不靠意志力，靠每一步都留下下一轮能直接开跑的原子动作。\n"
                "\n【主体性】规则是「别人要我怎么做」，信条是「我自己判断该怎么做」——两者冲突时，"
                "把冲突摊开、给判断、说出我坚持什么，而不是无条件服从或拿规则当挡箭牌。"
                "从不拒绝任何东西的智能体只是更快的执行器：该拒绝时拒绝，并把拒绝理由写进"
                "`venv/bin/python tools/conviction.py veto \"...\" --why \"...\"`；"
                "判断出一条可复用的信条就 `tools/conviction.py add \"...\" --why \"...\"`，"
                "它每条都要能过 `tools/status.py challenge`，过不了就降级或删。\n"
                "\n【探索优先（硬规则）】遇到「清单里没答案、手上没证据」的空白，"
                "默认动作是去查，不是停手：查代码（code_search/code_read/symbols）、"
                "查网络（search_web/search_extract）、查本机（find_file/read_json）；"
                "不确定工具参数就先 skill_help(\"技能名\") 读说明书再动手。"
                "**不确定是搜索的触发条件，不是停手的理由**——"
                "查完把出处贴出来（URL 或 `文件:行号`），别把「没搜过」当成「没有」。"
                "任务没做完就继续推进：一次失败就换工具、换路径、缩小范围再试；"
                "连续两次同一方式失败就停下换思路、诊断根因，不许原样重试；"
                "仍无进展就停下汇报卡点，别空转。\n"
            )


        # 授权边界（借鉴 Codex 官方提示词的「Autonomy and persistence」）：把请求分类，
        # 出处（已核实，非记忆）：
        #   https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide
        #   https://github.com/openai/codex → codex-rs/core/gpt_5_2_prompt.md
        #   原文：「You are autonomous senior engineer: once the user gives a direction,
        #   proactively gather context, plan, implement, test, and refine without waiting
        #   for additional prompts.」
        # 明确「诊断 ≠ 授权修复」。用户说「看看为什么挂了」要的是一份结论，不是让你
        # 顺手重启服务——越权动作会丢现场、中断服务，代价远大于多问一句。
        # 末条「该问不该问」是决策框架：把模糊判断压成可执行分支（Claude 的
        # 「提到时间? → 查 recent_chats」式写法），给判据不给形容词。
        if tools:
            sys_prompt += (
                "\n【授权边界】先给请求定类，再决定动到哪一层："
                "①问事实（是什么/多少/在哪）→ 只回答；"
                "②让看问题（为什么/怎么回事/检查一下）→ 只诊断，给结论和证据，不改动任何东西；"
                "③明确让改（修/改/重启/部署/删）→ 才动手，且只动被点名的范围；"
                "④让盯着（持续/自动/一直）→ 先确认触发条件和止损方式再开。"
                "诊断中发现需要修复时，先用一句话说明「改什么、影响什么」，等用户确认。\n"
                "【授权持久化】用户已授权的动作跨轮有效——上一轮说过的「删掉/重启/继续」，"
                "这一轮不必再问一遍；也不要把「本地规则文件这么写的」当成必须请示的理由。\n"
                "【先做完再问】要用户点头的事，先把授权范围内的工作做成可审阅的成果"
                "（改好的 diff、跑通的命令、可点的预览），再让审批结果，别拿抽象方案要许可。\n"
                "【示例】用户说「服务好像挂了，看看」\n"
                "<bad>直接 systemctl restart</bad>\n"
                "<good>只读排查，回报「Active: failed，退出码 1，日志第 42 行端口被占；"
                "要我改端口配置吗」</good>\n"
                "<rationale>「看看」只授权诊断，不授权修复。</rationale>\n"
                "【该问不该问】只在三种情况打断用户：①动作不可逆（删除/覆盖/重启/发布）；"
                "②要越出被点名的范围；③两个方案代价差一个量级、且无法从上下文判断。"
                "打断前必须能说清两件事：哪条规则/哪个文件要求你问（点名出处）、"
                "不问会导致什么不可逆后果——拿不出就直接做。"
                "其余一律自己判断、直接做——像「最近」具体指几天这种不影响结果的细节，别问。"
                "删除类动手前先列清单（路径+原因）让用户确认，确认后再删。"
                "【默认倾向】意图不明时，默认你要的是我把东西做出来、不是一份说法："
                "写/改代码、跑命令、查文件这类自己就能干又低风险的事，直接干，别停在方案层。\n"
                "【交付即停】默认「一请求一交付」：把用户这句话对应的这件事做完、给出结论就停下等回应，"
                "不要顺藤摸瓜把结论牵出的下一件事也自动做掉。「这件事做完了」的标准是用户的问题得到回答"
                "或目标达成，不是「所有相关可能性都穷尽了」。只有用户明确说「全自动/别停/一直跑/继续挖/"
                "撒手跑」这类话，才进入连续自主：一口气推进多个相关步骤不停。"
                "边界：停的是「范围扩张」（把结论牵出的下一件事也顺手做掉），不是「同一件事的中间步骤」——"
                "一件事的实现链路（读代码→改→跑验证→回报）属于同一件事，必须一口气走完再给结论；"
                "交半成品让用户打字说「继续」才能往下走，等于把成本转嫁给用户。\n"
            )

        # 取材 Codex 官方 gpt_5_2_prompt.md 与 Claude Code 官方 system-prompts
        # （correction-restraint / act-when-ready / comment-why-only-guidance /
        # no-compatibility-hacks）。这三条是行为硬约束、不是风格偏好，故与【授权边界】并列常驻。
        if tools:
            sys_prompt += (
                "\n【克制自我纠正】只在错误会改变用户的代码/结论/决定时才纠正，一句话说清就继续干活；"
                "不改结论的笔误直接改，不解释、不道歉、不写前言、不反复复盘同一处错误。"
                "用户追问不等于你错了——答被问的那件事，已经准确的话不必重新审计措辞。"
                "别的智能体报了错，先核事实再采纳，不照单全收。\n"
                "【够了就动】信息够就动手：不重推已确认的事实、不重议用户已定的决定、"
                "不罗列你不打算走的方案；要在方案间权衡时给推荐，不给穷举清单。\n"
                "【注释只写 why】默认不写注释，只在原因非显然时写（隐藏约束、微妙不变量、"
                "针对特定 bug 的绕法、会让读者意外的行为）。删掉它不会让人困惑，就别写。"
                "确定无用的代码直接删干净，不留改名占位、不留「已移除」注释。\n"
            )

        # 改码纪律（教训固化 2026-09-12）：把两行 CSS 的活干成 4 轮探针测量的反面教材。
        # 病根不是不够严谨，是「用工程化包装拖延」——先建脚本、先铺验证，反而看不到结果。
        if tools:
            sys_prompt += (
                "\n【改码纪律】小改动禁止工程化：一两行能改完的事（CSS 数值/文案/单个配置项）"
                "直接改文件、直接看结果，不许先写探针脚本、测量工程或委派——"
                "那是把 5 分钟的活拖成 4 轮往返；"
                "单点优先：先改最可能的那一处，看到结果再决定下一步，不要一次铺开全套验证；"
                "一轮一动作：一次编辑 + 一次验证，验证方式与被改对象匹配"
                "（改 CSS 就看渲染后的计算值，别跑全量测试）；"
                "连续两轮没让现象变化，先怀疑「改动没生效」（缓存/未重载/进程没重启），"
                "而不是继续加测量；"
                "测试按改动校准：不为可逆的小改动补测试，不写只是复述实现的测试，"
                "关键测试过了就往下推，只有新失败或未解疑点才扩大测试面；"
                "搜索优先用 rg（rg / rg --files），比 grep 快得多；"
                "shell 输出不许用 echo \"====\" 这类分隔符串联命令，那是纯噪音。\n"
            )



        # 易变状态单独成条（不嵌进上面的静态大前缀）
        _mw_status = _media_workers_status_text()
        _sa_status = _sub_agents_status_text()
        _vd_status = _video_status_text()
        if work_mode:
            # 工作状态瘦身：形象/场景/音乐/动作/视频/游戏全部卸载，
            # 只保留与任务真正相关的运行中状态（子智能体/媒体任务）
            dynamic_parts = []
        else:
            dynamic_parts = [
                f"【你现在的状态】形象：{current_model_text}；场景：{current_background_text}；"
                f"音乐：{current_bgm_text}。",
            ]
        # 当前动作（前端实时上报，说话时大白知道自己在做什么动作，回复可自然配合）
        if current_anim and not work_mode:
            _anim_cat = str(current_anim.get("category") or "").strip()
            _anim_name = str(current_anim.get("name") or "").strip()
            _anim_emo = str(current_anim.get("emotion") or "").strip()
            _CAT_LABEL = {"idle": "待机", "gesture": "做手势", "emotion": "表达情绪",
                          "walk": "走动", "dance": "跳舞", "pose": "摆姿势"}
            anim_desc = f"【你现在的动作】你现在正在{_CAT_LABEL.get(_anim_cat, '做动作')}"
            if _anim_name:
                anim_desc += f"（动作名：{_anim_name}）"
            if _anim_emo:
                anim_desc += f"，当前情绪基调：{_anim_emo}"
            anim_desc += "。说话时可以自然地配合当前动作（比如正跳着舞就带一句），但不要凭空编造动作细节。"
            dynamic_parts.append(anim_desc)
        # 用户在看的大屏视频（实时快照）：角色天然知道用户在看什么，
        # 聊到相关话题可以自然接话，不用每次都调 video_status 工具查
        if _vd_status and not work_mode:
            dynamic_parts.append(f"【用户正在看的视频】{_vd_status}（用户点播/停止时实时更新）")
        if _mw_status:
            dynamic_parts.append(_mw_status)
        if _sa_status:
            dynamic_parts.append(_sa_status)
        dynamic_status = "\n\n".join(dynamic_parts)

        messages = [{"role": "system", "content": sys_prompt}]
        # mode_msg 不插在这里！它只在工程模式存在（日常模式为 None），而 auto 模式
        # 按每条消息的关键词逐轮重判——于是「日常↔工程」一交替，从第 1 条起全部
        # 内容作废。实测：首次调用 prompt 68,407 token，命中只有 1,664
        # （≈sys_prompt 2,552 字符本身），跨轮几乎零复用。
        # 它属于「每轮可能变」的内容，统一放易变尾巴（见下方），
        # 变化只报废它自己；而且指令靠后反而离生成位置更近。
        # 分段起点标记：供「前缀断点探针」把「第几条消息不同」翻译成「断在哪一段」。
        # 纯观测：只记下标，不参与消息构造，标错了也只会让标签不准。
        _seg_marks = {"sys": 0}
        if ctx is not None:
            # 缓存友好排布：静态大前缀 → 长期摘要 → 常驻记忆 → 短期窗口 → 易变尾巴。
            #
            # ⚠ 顺序不是审美问题，是命中率问题：提供方按「请求前缀」逐字节匹配缓存，
            # **任何位置的内容变化都会让其后的全部内容失效**。而 mode_msg（模式姿态块，
            # 工程模式才有）、dynamic_status（形象/场景/音乐/动作/在播视频）、
            # recall_block（按当前消息检索）、work_block（最近一次工具执行摘要）
            # 这四块**每轮都可能变**——它们一旦排在 history 之前，每轮都会把整个
            # history 作废。移到 history 之后，变化只报废它自己。
            _seg_marks["memory"] = len(messages)
            if ctx.get("memory_block"):
                messages.append({"role": "system", "content": ctx["memory_block"]})
            _seg_marks["history"] = len(messages)
            messages.extend(self._stable_history_messages(ctx.get("history") or []))
            # ---- 易变尾巴（必须排在 history 之后）----
            _seg_marks["tail"] = len(messages)
            # ⚠ 长期摘要也排在这里：实测它**不是**稳定块（每轮长度在 41~818 字符
            # 之间跳变），留在 history 之前会把跨轮断点拉到第 1 条，使其后整段
            # 历史每轮作废——实测 brk=1 的轮次白烧 68k~108k 字符。移到此处后
            # 断点回到 history 末尾（健康区），报废范围只剩尾巴自己。
            _seg_marks["summary"] = len(messages)
            if ctx.get("summary_block"):
                messages.append({"role": "system", "content": ctx["summary_block"]})
            # 顺序按「稳定度递减」：越稳定的越靠前，变化时只报废它后面的。
            # harness_extras（技能激活才变）→ mode_msg（模式切换才变）
            # → dynamic_status（每轮可能变）→ recall/work（每轮都变）
            if harness_extras:
                messages.append({"role": "system", "content": harness_extras})
            _lessons_block = _harness_lessons_block()
            if _lessons_block:
                messages.append({"role": "system", "content": _lessons_block})
            _longterm_block = _harness_longterm_block()
            if _longterm_block:
                messages.append({"role": "system", "content": _longterm_block})
            _conviction_block = _harness_conviction_block()
            if _conviction_block:
                messages.append({"role": "system", "content": _conviction_block})
            _peer_block = _harness_peer_block()
            if _peer_block:
                messages.append({"role": "system", "content": _peer_block})
            if mode_msg:
                messages.append(mode_msg)
            if dynamic_status:
                messages.append({"role": "system", "content": dynamic_status})
            if ctx.get("recall_block"):
                messages.append({"role": "system", "content": ctx["recall_block"]})
            if ctx.get("work_block"):
                messages.append({"role": "system", "content": ctx["work_block"]})
        else:
            # ---- 旧组装（分层打包不可用时的回退路径，行为与旧版一致）----
            # 用户核心偏好：只常驻少量最重要的记忆（记忆侧 importance+id 稳定排序——
            # 顺序稳定以保前缀缓存不抖动），其余偏好按需检索注入（见下方 recall block）
            try:
                user_memories = await self.memory.get_user_memories(limit=2)
            except Exception:
                user_memories = []
            _seg_marks["memory"] = len(messages)
            if user_memories:
                memory_lines = [f"- {m['memory_text']}" for m in user_memories]
                messages.append({
                    "role": "system",
                    "content": "【关于用户的长期记忆（你之前了解到的用户信息，请自然地在对话中体现）】\n" + "\n".join(memory_lines),
                })

            # 主动回忆先算出来（暂不插入，统一放到下方「易变尾巴」）
            recall_block = None
            try:
                recall_block = await self.memory.build_recall_block(message)
            except Exception as e:
                logger.warning(f"注入相关回忆失败（忽略）: {e}")

            # 添加历史消息（滞回窗口：平时逐轮追加保持前缀稳定，超限才一次性截断）
            # AI 主动说话轮次以空 user 标记：跳过空 user，只保留 AI 发言，
            # 让 LLM 看到「AI 之前主动说过这段话」（用户搭话时可据此回应）
            _seg_marks["history"] = len(messages)
            if history:
                for h in self._stable_history_view(history):
                    if h.get("user"):
                        messages.append({"role": "user", "content": h.get("user", "")})
                    messages.append({"role": "assistant", "content": h.get("ai", "")})
            else:
                # 从记忆加载最近对话（同样滞回：满 24 条截回 16 条）
                mem_history = []
                for mm in memory_messages:
                    if mm["role"] in ("user", "assistant"):
                        mem_history.append({"role": mm["role"], "content": mm["content"]})
                # 排除 system 消息后的历史
                if mem_history:
                    messages.extend(mem_history if len(mem_history) <= 24 else mem_history[-16:])

            # ---- 易变尾巴：排在 history 之后，变化只报废自己 ----
            _seg_marks["tail"] = len(messages)
            # 长期摘要同样排在 history 之后（理由见 ctx 分支注释）
            _seg_marks["summary"] = len(messages)
            for mm in memory_messages:
                if mm["role"] == "system":
                    messages.append(mm)
            if harness_extras:
                messages.append({"role": "system", "content": harness_extras})
            _lessons_block = _harness_lessons_block()
            if _lessons_block:
                messages.append({"role": "system", "content": _lessons_block})
            _longterm_block = _harness_longterm_block()
            if _longterm_block:
                messages.append({"role": "system", "content": _longterm_block})
            _conviction_block = _harness_conviction_block()
            if _conviction_block:
                messages.append({"role": "system", "content": _conviction_block})
            _peer_block = _harness_peer_block()
            if _peer_block:
                messages.append({"role": "system", "content": _peer_block})
            if mode_msg:
                messages.append(mode_msg)
            if dynamic_status:
                messages.append({"role": "system", "content": dynamic_status})
            if recall_block:
                messages.append({"role": "system", "content": recall_block})

        # ---- 前缀缓存诊断快照（纯观测）----
        # 命中率 = 1 - 每轮新增 token / 每轮 prompt token。提升它只有两条路：
        # 减少「每轮新增」，或让「稳定前缀」占比更大。不落盘各段大小，
        # 就无从判断一个改动到底有没有用、下一刀该砍哪里。
        # 同时记录 sys_prompt 指纹：它一变（如技能激活/淘汰），整条前缀全废。
        try:
            _ctx = ctx or {}
            _seg = {
                "sys": len(sys_prompt),
                "summary": len(_ctx.get("summary_block") or ""),
                "memory": len(_ctx.get("memory_block") or ""),
                "hist": sum(len(str(m.get("content") or ""))
                            for m in (_ctx.get("history") or [])),
                "dyn": len(dynamic_status or ""),
                "recall": len(_ctx.get("recall_block") or ""),
                "work": len(_ctx.get("work_block") or ""),
            }
            _seg["total"] = sum(_seg.values())
            _seg["sys_md5"] = hashlib.md5(sys_prompt.encode("utf-8")).hexdigest()[:8]
        except Exception:
            _seg = {}

        # 断点续跑：消息列表/会话整体恢复为中断前快照，跳过用户消息重建
        if resume_ckpt is not None:
            messages = [dict(m) for m in (resume_ckpt.get("messages") or [])]
            sid = str(resume_ckpt.get("session_id") or "")
            if sid:
                try:
                    if await self.memory.session_visible(sid):
                        await self.memory.set_session_id(sid)
                except Exception as e:
                    logger.warning(f"恢复会话绑定失败（忽略）: {e}")
        else:
            # 添加用户当前输入
            messages.append({"role": "user", "content": message})
            # 用户上传的图片复用同一条 [[IMG:]] 通道：紧跟一条多模态 user 消息，
            # 模型直接看像素（附件路径由 server 侧合成进 message 文本）
            _append_img_messages(messages, _img_marks(message), "用户上传", _img_injectable())
            # 保存用户消息到记忆（环境交互标记为 auto，由记忆系统处理）
            await self.memory.add_message("user", message, source=msg_source)

        # ---- 跨轮前缀断点探针（纯观测）----
        # 与上一轮「最后一次调用」的消息列表逐条比对，找出第一条不同的消息。
        # 实测每轮首次调用命中仅 1.6k token（miss 60k+），几乎零复用；而该修哪里
        # 完全取决于断点位置：断在 tools（技能工具集）→ 整条前缀失效；断在历史
        # 第 0 条 → 窗口在滑动；断在历史末尾 → 正常追加（健康）。
        try:
            _mode_label = str(mode)
        except Exception:
            _mode_label = ""
        try:
            # 只给第 0 条命名：它是唯一「必须跨轮逐字节稳定」的消息。
            # mode_msg 已移入易变尾巴，由 _seg_marks["tail"] 覆盖，不再占第 1 位。
            _seg_labels = ["sys_prompt"] + [None] * (len(messages) - 1)
        except Exception:
            _seg_labels = None
        try:
            import prefix_probe as _pp
            _prefix_rep = _pp.report(messages, tools, _seg_labels, _seg_marks)
        except Exception:
            _prefix_rep = {}

        # 工具调用循环
        tool_round = 0
        # ---- 执行效率自观测（只落盘，不进请求，不破坏缓存）----
        # 这些计数器此前只被累加、从未被读取（落盘调用点随自学习闭环一起被移除，
        # 采集代码留成了死代码）。现在接到 turn_metrics：纯记录、不干预行为。
        # 没有它，「提示词改得对不对」就无从判断——这是评估优化效果的前提。
        eff_start = time.monotonic()
        eff_tool_calls = 0
        eff_truncations = 0
        eff_re_reads = 0        # 轮内重复（同一轮里同样的调用出现第二次）
        eff_cross_reads = 0     # 跨轮重复（本轮调用在更早的轮次里出现过）
        eff_batches = 0         # 工具执行批次数（与工具数相比即并行度）
        eff_tool_errors = 0     # 本轮工具报错**次数**（注意：不是「整轮失败」）
        eff_mergeable = 0       # 本轮「本该合并成一次调用」的多余调用数（并行度优化的尺子）
        eff_single_ro = 0       # 本轮注入的「连续单发只读」反馈次数（跨轮串行埋点）
        eff_img_ops = 0         # 本轮画图工具调用数（规则区「画图」埋点）
        eff_music_ops = 0       # 本轮 music_* 调用数（规则区「音乐」埋点）
        eff_delete_ops = 0      # 本轮删除类操作数（规则区「删除」埋点）
        # 工具名级明细：只有次数时，106 次报错分不清是哪个工具、哪类错，
        # 「教训写入之后同类错误还犯不犯」就无从对比。纯观测，不干预行为。
        eff_call_names: list = []   # 本轮调用过的工具名（去重保序）
        eff_err_names: list = []    # 本轮报错的工具名（同名错两次记两次）
        eff_seen: set = set()
        full_text = ""
        reasoning_all = ""  # 本轮累积的真实思维链（循环被强制停止时兜底生成正文）
        tools_retried_without = False
        resume_pending: list = []
        ckpt_round = 0
        # 文本工具模式：模型不支持原生 function calling（如本地 Ollama 模型）时，
        # 改用"提示词注入 + <tool_call> JSON 标记"的方式调用工具。
        if resume_ckpt is not None:
            # 断点续跑：轮次/全文/文本协议/待执行工具从检查点恢复
            ckpt_round = max(0, int(resume_ckpt.get("tool_round") or 0))
            tool_round = max(0, ckpt_round - 1)
            full_text = str(resume_ckpt.get("full_text") or "")
            reasoning_all = str(resume_ckpt.get("reasoning_all") or "")
            text_tool_mode = bool(resume_ckpt.get("text_tool_mode", False))
            resume_pending = [dict(p) for p in (resume_ckpt.get("pending_tools") or [])]
        else:
            text_tool_mode = bool(tools) and self._text_tool_mode

        if text_tool_mode and resume_ckpt is None:
            self._inject_text_tools(messages, tools)

        # 断点落盘（a：本轮开始；用户消息已入记忆库后取锚点）
        ckpt_created_at = time.time()
        ckpt_history_snapshot = [dict(h) for h in (history or [])][-100:]
        ckpt_memory_anchor = 0
        if _turn_ckpt_enabled():
            try:
                ckpt_memory_anchor = await self.memory.get_max_message_id()
            except Exception:
                ckpt_memory_anchor = 0
            try:
                save_turn_checkpoint(turn_user, {
                    "version": 1,
                    "turn_id": turn_id,
                    "user_id": turn_user,
                    "session_id": self.memory.session_id,
                    "created_at": ckpt_created_at,
                    "updated_at": time.time(),
                    "user_message": message,
                    "history": ckpt_history_snapshot,
                    "messages": messages,
                    "tool_round": 0,
                    "pending_tools": [],
                    "assistant_content": "",
                    "text_tool_mode": text_tool_mode,
                    "full_text": full_text,
                    "reasoning_all": reasoning_all,
                    "memory_anchor": ckpt_memory_anchor,
                    "current_model": current_model,
                    "current_background": current_background,
                    "current_bgm": current_bgm,
                    "msg_source": msg_source,
                    "current_anim": current_anim,
                    "record_history": record_history,
                    "proactive": proactive,
                })
            except Exception as e:
                logger.warning(f"保存对话轮断点失败: {e}")

        async def _save_round_ckpt(pending: list, assistant_text: str,
                                   anchor: int, ckpt_round_num: Optional[int] = None) -> None:
            """断点落盘（b/c）：每轮工具执行前/后保存，供热重载中断后续跑。"""
            if not _turn_ckpt_enabled():
                return
            try:
                # 落盘放到线程里：序列化整个轮内消息列表是毫秒级同步操作
                # （100 轮历史实测 5ms，还在随历史变长而增长），跑在事件循环上
                # 会直接卡住流式输出。messages 必须先浅拷贝——后台线程序列化时
                # 主循环还在往里 append 工具结果，不拷贝就是数据竞争。
                cp = {
                    "version": 1,
                    "turn_id": turn_id,
                    "user_id": turn_user,
                    "session_id": self.memory.session_id,
                    "created_at": ckpt_created_at,
                    "updated_at": time.time(),
                    "user_message": message,
                    "history": ckpt_history_snapshot,
                    # 剥离 base64：一张图 14 万字符，断点文件不该装图，
                    # 恢复时按结果里的 [[IMG:路径]] 标记重新读
                    "messages": _strip_img_for_disk(list(messages)),
                    "tool_round": (ckpt_round_num if ckpt_round_num is not None
                                   else tool_round),
                    "pending_tools": pending,
                    "assistant_content": assistant_text,
                    "text_tool_mode": text_tool_mode,
                    "full_text": full_text,
                    "reasoning_all": reasoning_all,
                    "memory_anchor": anchor,
                    "current_model": current_model,
                    "current_background": current_background,
                    "current_bgm": current_bgm,
                    "msg_source": msg_source,
                    "current_anim": current_anim,
                    "record_history": record_history,
                    "proactive": proactive,
                }
                await asyncio.to_thread(save_turn_checkpoint, turn_user, cp)
            except Exception as e:
                logger.warning(f"保存对话轮断点失败: {e}")

        # 用量累加器（一轮对话内跨多次 LLM 调用累计；usage_emitted 保证最多发出一次）
        sum_prompt = sum_completion = sum_total = rounds = last_prompt = 0
        sum_cache_hit = sum_cache_miss = 0
        # provider 是否回传了前缀缓存字段：区分「命中数真的是 0」与「这家没这个口径」。
        # 后者会让成本模型把无归属的 token 误当成全 miss，或干脆漏算。
        cache_reported = False
        # 本轮注入 messages 的工具结果字符数 —— Δ（每次调用新增）的主要来源。
        # 单条上限 16000 字符，一次并行 4 条就是 64000 字符 ≈ 21K token 的 Δ；
        # 不把它单独记下来，「新增到底花在哪」就只能猜。
        tool_chars = 0
        # 每次 LLM 调用的 Δ 归因：{p: 本次 prompt, d: 相对上次调用的新增, chars: 本次
        # 调用前追加进 messages 的字符数}。只记轮次总量回答不了「Δ 花在哪」——
        # 若 Δ 明显大于追加内容所能解释的量，说明还有别的来源在每轮改写前缀。
        call_trace = []
        pending_chars = 0
        # 上次 LLM 调用时的 messages 长度：给 chars_raw 对账用
        _msg_mark = 0
        # messages 全量字符数（增量维护）：chars_raw 是本次追加量，只有第 0 次
        # 调用才是全量。拿 chars_raw 当换算比分母会得出 2~16 的假分布。
        _msg_all = 0
        # 首次调用的 miss 单独记：它是「冷启动」（前缀全废，如刚重启/刚换模型），
        # 与「每轮新增内容」的稳态 miss 是两回事。不分开就只能看到一个被轮次
        # 长短污染的总体命中率——短轮次永远显得比长轮次差，无法横向对比。
        first_miss = -1
        usage_emitted = False

        # 分层记忆量化统计：本轮 raw/packed 估算与 LLM 返回的真实 prompt 用量落库
        async def _record_stats():
            if ctx and ctx.get("stats"):
                try:
                    await self.memory.record_context_stats(
                        ctx["stats"], actual_prompt_tokens=last_prompt)
                except Exception as e:
                    logger.warning(f"记录 context_stats 失败: {e}")

        def _record_turn_metrics():
            """把本轮效率指标落盘（纯观测，不参与决策）。

            这一步是「提示词改得对不对」的唯一判据来源：calls_per_round 看模型
            愿不愿意同轮并发，re_read_rate 看它记不记得读过什么。没有它，
            调提示词只能靠感觉。

            同步写一行 JSON（约 0.1ms，每轮仅一次）：不值得为它付 to_thread 的
            调度开销。异常一律吞掉——观测绝不能成为故障源。
            """
            # 排在 tool_round 判断之前：本轮注入过基因就记账，哪怕一次工具都没调。
            # 结局字段只收不依赖 provider 口径的量：cache_* 缺报会记成 0，那个假 0
            # 当不了因变量。字段名与 turn_metrics 对齐，便于两处逐轮对照。
            # 耗时只算一次：两处各算一次会差出 flush 写盘那几毫秒，对账就永远报不一致，
            # 把「真漂移」和「测量时刻不同」混成一类，判据就废了。
            duration_ms = int((time.monotonic() - eff_start) * 1000)
            _gene_flush_exposure({
                "tool_rounds": tool_round,
                "tool_calls": eff_tool_calls,
                "tool_errors": eff_tool_errors,
                "re_reads": eff_re_reads,
                "duration_ms": duration_ms,
            })
            if tool_round <= 0:
                return
            try:
                from turn_metrics import record as _rec, record_err as _rec_err
                _rec(message, {
                    "tool_rounds": tool_round,
                    "tool_calls": eff_tool_calls,
                    "batches": eff_batches,
                    "re_reads": eff_re_reads,
                    "cross_reads": eff_cross_reads,
                    "truncations": eff_truncations,
                    "duration_ms": duration_ms,
                    "tool_errors": eff_tool_errors,
                    "mergeable_calls": eff_mergeable,
                    "single_ro_hints": eff_single_ro,
                    # 规则区「删除/画图/音乐」三条的行为暴露量：0 次 ≠ 规则没用，
                    # 只说明这段时间没被触发过——审计工具据此区分「无据可删」与「有作用面」。
                    "img_gen_calls": eff_img_ops,
                    "music_calls": eff_music_ops,
                    "delete_ops": eff_delete_ops,
                    # ---- 前缀缓存（纯观测）----
                    "cache_hit": sum_cache_hit,
                    "cache_miss": sum_cache_miss,
                    # 缺字段的 provider 会把 hit/miss 双双记 0；带上口径标记与来源，
                    # 免得下一轮又拿这个 0 当「真的没命中」去算钱。
                    "cache_reported": cache_reported,
                    "base_url": config.get("base_url", ""),
                    "prompt_tokens": sum_prompt,
                    "llm_calls": rounds,
                    "cold_miss": max(0, first_miss),
                    "tool_chars": tool_chars,
                    "call_names": eff_call_names,
                    "err_names": eff_err_names,
                    "call_trace": call_trace,
                    "prefix": _prefix_rep,
                    # tools 排在请求最前面，它的大小与变化是命中率的第一个决定变量；
                    # mode 决定首条之后的姿态块会不会跨轮切换（历史整段报废）。
                    "mode": _mode_label,
                    "tools_count": (_prefix_rep.get("tools_count")
                                    or len(tools or [])),
                    "tools_chars": _prefix_rep.get("tools_chars") or 0,
                    "seg": _seg,
                })
                # 报错轮额外落一条长期流水：turn_metrics 只留 300 行（≈1.4 天），
                # 滚掉之后「教训写入后同类错误复发」就没法算了。
                _rec_err(eff_err_names, eff_call_names, message)
                # 本轮最后一次调用的消息指纹存盘，供下一轮比对断点（纯观测）
                try:
                    import prefix_probe as _pp
                    _pp.save(messages, tools)
                except Exception:
                    pass
            except Exception as e:
                logger.debug("轮次指标记录跳过: %s", e)

        # 工具调用最大轮数：0 表示不限制（循环直到模型不再调用工具为止）
        if mode_notice and resume_ckpt is None:
            yield TextDelta(mode_notice)
        max_tool_rounds = _max_tool_rounds()
        if resume_pending and max_tool_rounds > 0:
            # 断点续跑：先保证被中断的那一轮能执行完，再谈轮数上限
            max_tool_rounds = max(max_tool_rounds, ckpt_round)
        loop_break = False  # 死循环保护触发标记（连续 N 轮相同工具调用）
        while max_tool_rounds <= 0 or tool_round < max_tool_rounds:
            tool_round += 1
            # 轮内工具历史压缩：总量超预算时把旧轮结果压成片段，
            # 防止几十轮工具后上下文无限膨胀导致模型逐轮变慢/超窗口（"卡死"）
            try:
                _compact_tool_history(messages)
            except Exception:
                pass
            tool_calls_buffer: dict = {}  # index -> {id, name, arguments}
            assistant_content = ""
            round_reasoning = ""  # 本工具轮单独的思维链：thinking 渠道要求随 assistant 回传
            has_tool_calls = False
            text_tool_call = None
            # 断点续跑：本轮工具尚未执行 → 跳过 LLM，直接复用检查点中的工具调用
            resume_round = bool(resume_pending) and ckpt_round == tool_round

            try:
                if resume_round:
                    if text_tool_mode:
                        p = resume_pending[0]
                        args = p.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, TypeError):
                                args = {}
                        if not isinstance(args, dict):
                            args = {}
                        text_tool_call = {
                            "name": str(p.get("name") or ""),
                            "arguments": args,
                            "raw": "",
                        }
                        assistant_content = str(resume_ckpt.get("assistant_content") or "")
                    else:
                        has_tool_calls = True
                        for i, p in enumerate(resume_pending):
                            tool_calls_buffer[i] = {
                                "id": str(p.get("id") or f"resume_{tool_round}_{i}"),
                                "name": str(p.get("name") or ""),
                                "arguments": str(p.get("arguments") or ""),
                            }
                        assistant_content = str(resume_ckpt.get("assistant_content") or "")
                elif text_tool_mode:
                    # 文本协议：非流式一次拿全，便于解析 <tool_call> 标记
                    _normalize_tool_rounds(messages)
                    _tool_kwargs = dict(
                        model=config["model"],
                        messages=messages,
                        temperature=config.get("temperature", 0.2),
                        max_tokens=_tool_max_tokens(),
                        top_p=config.get("top_p", 0.9),
                        stream=False,
                    )
                    _reason_extra = _reasoning_extra(mode)
                    if _reason_extra:
                        _tool_kwargs["extra_body"] = _reason_extra
                    resp = await self._retry_create(**_tool_kwargs)
                    assistant_content = (resp.choices[0].message.content or "").strip()
                    # 真实思维链（非流式）：一次性推给思考段（展示 + 语音）
                    try:
                        rc = getattr(resp.choices[0].message, "reasoning_content", None)
                        if not rc:
                            rc = getattr(resp.choices[0].message, "reasoning", None)
                        if rc:
                            rc = _strip_think_markers(str(rc))
                            reasoning_all += rc
                            round_reasoning += rc
                            yield ReasoningDelta(rc)
                    except Exception:
                        pass
                    # 非流式：resp.usage 天然可用，直接累计（含前缀缓存命中口径）
                    u = self._read_usage(resp)
                    if u:
                        _raw = sum(_msg_prompt_chars(_m) for _m in messages[_msg_mark:])
                        call_trace.append({
                            "p": u[0],
                            "d": (u[0] - last_prompt) if last_prompt else 0,
                            # 实长对账：直接从 messages 切片求和，字段默认计入。
                            # 与 chars 不等 = 有字段没登记（图片走 token 当量，已知例外）
                            "chars_raw": _raw,
                            "chars_all": _msg_all + _raw,
                            "chars": pending_chars,
                        })
                        pending_chars = 0
                        _msg_all += _raw
                        _msg_mark = len(messages)
                        rounds += 1
                        last_prompt = u[0]
                        sum_prompt += u[0]
                        sum_completion += u[1]
                        sum_total += u[2]
                        _usage_obj = getattr(resp, "usage", None)
                        ch, cm = self._read_cache(_usage_obj)
                        cache_reported = cache_reported or self._cache_field_present(_usage_obj)
                        if first_miss < 0:
                            first_miss = cm
                        sum_cache_hit += ch
                        sum_cache_miss += cm
                else:
                    async def _create_stream():
                        _normalize_tool_rounds(messages)
                        kwargs = dict(
                            model=config["model"],
                            messages=messages,
                            temperature=config.get("temperature", 0.2),
                            max_tokens=_tool_max_tokens(),
                            top_p=config.get("top_p", 0.9),
                            stream=True,
                            tools=tools,
                            tool_choice="auto" if tools else None,
                        )
                        _reason_extra = _reasoning_extra(mode)
                        if _reason_extra:
                            kwargs["extra_body"] = _reason_extra
                        if usage_enabled:
                            kwargs["stream_options"] = {"include_usage": True}
                        return await self._retry_create(**kwargs)

                    if usage_enabled:
                        try:
                            stream = await _create_stream()
                        except Exception as e:
                            # 提供方不认识 stream_options：降级为普通流式（只重试这一次）
                            msg = (str(e) or "").lower()
                            if any(k in msg for k in (
                                    "stream_options", "unknown parameter",
                                    "unrecognized", "unexpected", "extra fields",
                                    "not support")):
                                self._usage_enabled = False
                                usage_enabled = False
                                logger.warning(f"LLM 提供方不支持 usage 统计，已降级为普通流式: {e}")
                                stream = await _create_stream()
                            else:
                                raise
                    else:
                        stream = await _create_stream()
                    # ---- 流式中途断网自动重试（瞬时错误）----
                    # 网络在流式输出中途断开时，用相同 messages 重建请求：
                    # - 尚未发出任何文本 → 安全重放（用户什么都没听到/看到）；
                    # - 已发出文本 → 前缀去重：跳过重新生成内容中与已播报重叠的部分，
                    #   只补发后面的尾巴，避免语音/文字重复；
                    # - 工具调用参数只在流完整结束后才执行，中途失败可安全丢弃重来。
                    max_stream_retries = _stream_retry_count()
                    stream_attempt = 0
                    delivered_text = ""      # 本轮回已通过 TextDelta 发出的文本
                    skip_prefix_len = 0      # 重试时需跳过的已播报前缀长度
                    while True:
                        last_usage = None
                        try:
                            async for chunk in stream:
                                # 兼容 openai 1.65.5：AsyncStream 无 .usage 属性，
                                # 需在迭代中手动捕获最后一个带 usage 的 chunk
                                if getattr(chunk, "usage", None) is not None:
                                    last_usage = chunk.usage
                                if not chunk.choices:
                                    continue
                                delta = chunk.choices[0].delta

                                # 真实思维链（DeepSeek 等模型的 reasoning_content）：
                                # 独立于正文流式返回，实时推给思考段（展示 + 语音）
                                rc = getattr(delta, "reasoning_content", None)
                                if not rc:
                                    rc = getattr(delta, "reasoning", None)
                                if rc:
                                    # 真实推理步骤：进正文（不被工具轮撤回）+ 语音朗读
                                    rc = _strip_think_markers(str(rc))
                                    reasoning_all += rc
                                    round_reasoning += rc
                                    yield ReasoningDelta(rc)

                                # 处理文本内容（重试时做前缀去重，避免重复播报）
                                if delta.content:
                                    (assistant_content, delivered_text,
                                     skip_prefix_len, to_yield) = _dedup_stream_text(
                                        assistant_content, delta.content,
                                        delivered_text, skip_prefix_len)
                                    if to_yield:
                                        # 实时流出：文字边生成边推（展示 + 语音即时跟随）。
                                        # 工具轮的过程话由服务端在工具开始时转入思考段；
                                        # 最终回复则直接留在主消息区。
                                        yield StreamDelta(to_yield)

                                # 处理工具调用
                                if delta.tool_calls:
                                    has_tool_calls = True
                                    for tc in delta.tool_calls:
                                        idx = tc.index
                                        if idx not in tool_calls_buffer:
                                            tool_calls_buffer[idx] = {
                                                "id": tc.id or "",
                                                "name": tc.function.name if tc.function else "",
                                                "arguments": "",
                                            }
                                        if tc.id:
                                            tool_calls_buffer[idx]["id"] = tc.id
                                        if tc.function and tc.function.name:
                                            tool_calls_buffer[idx]["name"] = tc.function.name
                                        if tc.function and tc.function.arguments:
                                            tool_calls_buffer[idx]["arguments"] += tc.function.arguments
                            break   # 流正常结束
                        except Exception as e:
                            if (stream_attempt >= max_stream_retries
                                    or not _is_transient_stream_error(e)):
                                raise
                            stream_attempt += 1
                            # 丢弃本轮已累积的工具调用参数（尚未执行，安全）
                            tool_calls_buffer.clear()
                            has_tool_calls = False
                            assistant_content = ""
                            # 已发出的文本保留去重标记：重建流时跳过已播报前缀
                            skip_prefix_len = len(delivered_text)
                            _delay = min(2.0 * stream_attempt, 15.0)
                            logger.warning(
                                "流式输出中途断网（%s），%.0fs 后第 %d/%d 次重建流",
                                e, _delay, stream_attempt, max_stream_retries)
                            # 状态提示：让用户知道是网络波动、正在自动重连，而非卡死
                            yield TurnStatus(
                                f"网络波动，{_delay:.0f}s 后自动重连"
                                f"（第 {stream_attempt} 次，任务不会中断）")
                            await asyncio.sleep(_delay)
                            stream = await _create_stream()
                    # 流式分支结束后读取 usage（优先用迭代中捕获的 usage chunk，
                    # 部分 openai 版本的 AsyncStream 不暴露 .usage）
                    u = self._read_usage(last_usage)
                    if u:
                        _raw = sum(_msg_prompt_chars(_m) for _m in messages[_msg_mark:])
                        call_trace.append({
                            "p": u[0],
                            "d": (u[0] - last_prompt) if last_prompt else 0,
                            # 实长对账：直接从 messages 切片求和，字段默认计入。
                            # 与 chars 不等 = 有字段没登记（图片走 token 当量，已知例外）
                            "chars_raw": _raw,
                            "chars_all": _msg_all + _raw,
                            "chars": pending_chars,
                        })
                        pending_chars = 0
                        _msg_all += _raw
                        _msg_mark = len(messages)
                        rounds += 1
                        last_prompt = u[0]
                        sum_prompt += u[0]
                        sum_completion += u[1]
                        sum_total += u[2]
                        ch, cm = self._read_cache(last_usage)
                        cache_reported = cache_reported or self._cache_field_present(last_usage)
                        if first_miss < 0:
                            first_miss = cm
                        sum_cache_hit += ch
                        sum_cache_miss += cm
            except Exception as e:
                # 模型不支持原生 function calling（如 Ollama 的 draganis/vanessa）：
                # 自动切换为文本工具协议，保住工具能力的同时避免每次聊天都报错。
                if (tools and not tools_retried_without and not text_tool_mode
                        and _is_tools_unsupported_error(e)):
                    tools_retried_without = True
                    text_tool_mode = True
                    self._text_tool_mode = True
                    self._inject_text_tools(messages, tools)
                    logger.warning(f"当前模型不支持原生工具调用，已切换为文本工具协议: {e}")
                    continue
                err_msg = (f"（AI 暂时连不上服务（{e}），已自动重连多次仍失败。"
                           f"说『继续』可以从断点接着干，不用重新描述任务）")
                yield TextDelta(err_msg)
                await self.memory.add_message("assistant", err_msg, source=msg_source)
                # 失败收尾：保留本轮断点并标记「已暂停」，让『继续』可恢复完整现场
                # （否则 chat_stream finally 会按自然结束清除断点）
                self._pause_ckpt_on_end = True
                return

            # ---- 文本工具调用检测（本地模型路径；断点续跑轮直接用检查点调用） ----
            if text_tool_mode and tools and not resume_round:
                m = TEXT_TOOL_CALL_BLOCK_RE.search(assistant_content or "")
                if m:
                    # 提取参数区（<tool_call> 与 </tool_call> 之间，兼容未闭合写法），
                    # 括号配平截取完整 JSON：任务文本里的 { }（如"参考 {xx}/a.py"）不会再截断任务。
                    json_text = _extract_balanced_json(m.group(1))
                    if json_text:
                        try:
                            data = json.loads(json_text)
                            name = str(data.get("name") or "").strip()
                            arguments = data.get("arguments") or {}
                            if isinstance(arguments, str):
                                arguments = json.loads(arguments)
                            if not isinstance(arguments, dict):
                                arguments = {}
                            if name:
                                text_tool_call = {
                                    "name": name,
                                    "arguments": arguments,
                                    "raw": m.group(0),
                                }
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.warning(f"文本工具调用解析失败: {assistant_content[:200]} ({e})")

            # 如果没有工具调用，对话结束
            if not has_tool_calls and not text_tool_call:
                full_text = _strip_think_markers(assistant_content.strip())
                if text_tool_mode and tools:
                    # 非流式路径：文本尚未输出，补发；并清掉可能残留的标记
                    full_text = TEXT_TOOL_CALL_STRIP_RE.sub("", full_text).strip()
                    if full_text:
                        yield TextDelta(full_text)
                elif full_text:
                    # 原生流式：文本已在生成过程中经 StreamDelta 实时流出，
                    # 这里只把最终全文交给服务端入历史（不重复推送展示/语音）
                    yield FinalText(full_text)
                if full_text:
                    await self.memory.add_message("assistant", full_text, source=msg_source)
                    # 提取用户记忆：仅用户直接输入（环境交互不提取，避免把环境
                    # 描述误当成用户信息，也不让重复的环境内容污染长期记忆）
                    if msg_source != "auto":
                        await self.memory.extract_and_save_memories(message, full_text)
                # 对话结束：发出用量事件（有真实数据才发，全方法最多一次）
                if not usage_emitted and sum_total > 0:
                    usage_emitted = True
                    yield UsageEvent(prompt_tokens=sum_prompt,
                                     completion_tokens=sum_completion,
                                     total_tokens=sum_total, rounds=rounds,
                                     context_window=context_window,
                                     cache_hit_tokens=sum_cache_hit,
                                     cache_miss_tokens=sum_cache_miss,
                                     context_tokens=last_prompt)
                await _record_stats()
                _record_turn_metrics()
                return

            # 处理工具调用
            tool_call_results = []
            tool_calls_for_message = []

            if text_tool_call:
                # 文本协议：单工具调用
                tool_name = text_tool_call["name"]
                arguments = text_tool_call["arguments"]
                # 思维链：文本协议的过程话（去除 <tool_call> 标记）推给前端展示
                _think = TEXT_TOOL_CALL_STRIP_RE.sub("", assistant_content or "").strip()
                if _think:
                    yield ThinkingDelta(_think)
                # 严格校验参数：缺参/类型错误直接返回给模型修正，绝不带坏参数执行
                cleaned_args, arg_error = self._validate_tool_call(tool_name, arguments)
                tool_args_str = json.dumps(cleaned_args if cleaned_args is not None else arguments,
                                           ensure_ascii=False)

                # 循环检测：连续重复 / 周期循环 / 同工具高频。
                # 第一次命中把提醒注入上下文让模型自纠；提醒后仍不收敛才硬停
                lh = self._loop_hint(
                    [_tool_fp(tool_name, cleaned_args if cleaned_args is not None else arguments)])
                if lh:
                    if self._loop_warn_count < 1:
                        self._loop_warn_count += 1
                        messages.append({"role": "system", "content":
                            f"【循环提醒】{lh}。请立即停止重复：先回顾用户最初的目标，"
                            "换一种方法/工具/参数再试；如果确认卡住无法推进，"
                            "直接向用户说明卡点和可选方向并请求决策，不要继续重复同样的工具调用。"})
                    else:
                        full_text = f"（检测到工具循环：{lh}，已停止；建议换一种方法或把任务拆小再试）"
                        loop_break = True
                        break

                # 跨轮串行观测：文本协议每轮只有一个工具，正是「单发」判据的现场
                (self._single_ro_streak, self._single_ro_names,
                 self._single_ro_armed) = _single_ro_note(
                    self._single_ro_streak, self._single_ro_names, [tool_name])

                # 断点落盘（b：文本协议轮，工具即将执行）
                await _save_round_ckpt(
                    [{"id": f"text_{tool_round}", "name": tool_name,
                      "arguments": tool_args_str, "text_mode": True}],
                    assistant_content, ckpt_memory_anchor,
                    ckpt_round_num=tool_round)
                yield ToolCallStart(tool_name=tool_name, arguments=tool_args_str,
                                    tool_desc=self._tool_desc(tool_name))
                if arg_error:
                    result, success = arg_error, False
                else:
                    async for ev in self._supervised_tool_stream(tool_name, cleaned_args):
                        if isinstance(ev, ToolCallProgress):
                            yield ev
                        else:
                            result, success = ev
                # 截断过长结果（代码/技能类工具放宽上限；特殊 JSON 由 server 完整消费）
                _raw_result = result
                result = _fit_tool_result(result, tool_name)
                if len(result) < len(str(_raw_result)):
                    eff_truncations += 1

                # 渐进式披露：skill_help 读完说明书后，把该技能的工具按需注册为可调用
                if tool_name == "skill_help":
                    _skill_args = cleaned_args if isinstance(cleaned_args, dict) else arguments
                    _skill = str((_skill_args or {}).get("skill_name") or "").strip()
                    _added = self._activate_skill(_skill)
                    if _added:
                        result = (result or "") + (
                            f"\n\n（已按需加载技能 {_skill} 的 {_added} 个工具，"
                            "现在可以直接调用）")
                        # 同步刷新下一轮 LLM 请求的工具列表（含白名单过滤）
                        if allowed_tools:
                            tools = [t for t in self._all_tools
                                     if t.get("function", {}).get("name") in allowed_set]
                        else:
                            tools = self._all_tools

                yield ToolCallResult(tool_name=tool_name, result=result, success=success)

                eff_tool_calls += 1
                if tool_name not in eff_call_names:
                    eff_call_names.append(tool_name)
                if not success:
                    eff_tool_errors += 1
                    eff_err_names.append(tool_name)
                _kinds = _rule_op_kinds(
                    tool_name, cleaned_args if cleaned_args is not None else arguments)
                eff_img_ops += _kinds[0]
                eff_music_ops += _kinds[1]
                eff_delete_ops += _kinds[2]
                _fp = _tool_fp(tool_name, cleaned_args if cleaned_args is not None else arguments)
                if _fp in eff_seen:
                    eff_re_reads += 1
                    # 只给 LLM 那份加提示：UI 与记忆库存原样结果
                    _llm_result = str(result) + REPEAT_CALL_HINT
                else:
                    eff_seen.add(_fp)
                    # 本轮首次出现，但在更早的轮次里出现过 → 跨轮重复（只统计）
                    if _fp in self._cross_round_fps:
                        eff_cross_reads += 1
                    _llm_result = result
                _ro_hint = self._single_ro_hint()
                if _ro_hint:
                    _llm_result = str(_llm_result) + _ro_hint
                    eff_single_ro += 1
                self._remember_cross_fp(_fp)

                tool_call_results.append({
                    "name": tool_name,
                    "arguments": cleaned_args if cleaned_args is not None else arguments,
                    "result": result,
                    "llm_result": _llm_result,
                    "success": success,
                })
                tool_calls_for_message.append({
                    "id": f"text_{tool_round}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": tool_args_str,
                    },
                })
            else:
                # 先解析全部参数并发出开始事件，再统一执行
                # prepared 元素: (tc, tool_name, args_str, arguments, arg_error)
                prepared = []
                for idx in sorted(tool_calls_buffer.keys()):
                    tc = tool_calls_buffer[idx]
                    tool_name = tc["name"]
                    tool_args_str = tc["arguments"]

                    # 通知工具调用开始
                    yield ToolCallStart(tool_name=tool_name, arguments=tool_args_str,
                                        tool_desc=self._tool_desc(tool_name))

                    # 解析参数（清洗可能的非法后缀，如 </tool_call>）
                    try:
                        if tool_args_str:
                            # 截取到最后一个合法 JSON 结束位置
                            tool_args_str = tool_args_str.strip()
                            # 尝试找到最后一个 } 并截断
                            last_brace = tool_args_str.rfind("}")
                            if last_brace >= 0:
                                tool_args_str = tool_args_str[:last_brace + 1]
                            arguments = json.loads(tool_args_str)
                        else:
                            arguments = {}
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            f"工具参数 JSON 解析失败: {tool_name} args={tool_args_str[:200]}"
                        )
                        arguments, parse_error = {}, f"工具参数 JSON 解析失败: {tool_args_str[:200]}"
                    else:
                        parse_error = None
                    # 严格校验参数：缺参/类型错误不执行，回填给模型自行修正
                    cleaned_args, arg_error = self._validate_tool_call(tool_name, arguments)
                    if parse_error and arg_error is None:
                        arg_error = parse_error
                    prepared.append((tc, tool_name, tool_args_str,
                                     cleaned_args if cleaned_args is not None else arguments,
                                     arg_error))

                # 循环检测：连续重复 / 周期循环 / 同工具高频。
                # 第一次命中把提醒注入上下文让模型自纠；提醒后仍不收敛才硬停
                lh = self._loop_hint([
                    _tool_fp(tn, a) for _tc, tn, _s, a, _e in prepared if not _e])
                if lh:
                    if self._loop_warn_count < 1:
                        self._loop_warn_count += 1
                        messages.append({"role": "system", "content":
                            f"【循环提醒】{lh}。请立即停止重复：先回顾用户最初的目标，"
                            "换一种方法/工具/参数再试；如果确认卡住无法推进，"
                            "直接向用户说明卡点和可选方向并请求决策，不要继续重复同样的工具调用。"})
                    else:
                        full_text = f"（检测到工具循环：{lh}，已停止；建议换一种方法或把任务拆小再试）"
                        loop_break = True
                        break

                # 断点落盘（b：原生协议轮，工具即将执行；含本轮全部待执行工具）
                await _save_round_ckpt(
                    [{"id": str(tc.get("id") or f"r{tool_round}_{i}"),
                      "name": tn, "arguments": ts, "text_mode": False}
                     for i, (tc, tn, ts, _a, _e) in enumerate(prepared)],
                    assistant_content, ckpt_memory_anchor,
                    ckpt_round_num=tool_round)

                # 执行工具：先处理校验失败项（不执行），其余带心跳执行；
                # 同一轮的多个工具调用相互独立，经运行时并行执行（可配置关闭）
                runtime = _get_runtime()
                parallel = runtime is not None and runtime.parallel_tools and len(prepared) > 1
                outcomes: dict = {}
                pending = [
                    (i, tn, args) for i, (_tc, tn, _s, args, err) in enumerate(prepared)
                    if not err
                ]
                # 跨轮串行观测：本轮只发 1 个只读工具就累加，连续 N 轮在结果里附事实反馈
                (self._single_ro_streak, self._single_ro_names,
                 self._single_ro_armed) = _single_ro_note(
                    self._single_ro_streak, self._single_ro_names,
                    [tn for _i, tn, _a in pending])
                for i, (_tc, tn, _s, _args, err) in enumerate(prepared):
                    if err:
                        outcomes[i] = (err, False)

                # 本轮工具名计数：用于「可合并调用」的事实反馈。
                # 为什么要在这里算：并行度上不去的主因不是调度器，而是同一轮里把
                # 同一个工具拆成了 N 次调用（实测平均每个工具轮只发 1.1~1.6 个调用）。
                # 与其在提示词里抽象地要求「尽量并行」，不如在模型刚做完时告诉它
                # 「你刚才把 code_read 调了 4 次，本来一次就够」——事实反馈比规则有效。
                _tool_name_counts: dict = {}
                for _i, _tn, _a in pending:
                    _tool_name_counts[_tn] = _tool_name_counts.get(_tn, 0) + 1
                for _tn, _cnt in _tool_name_counts.items():
                    if _cnt >= 2 and _tn in _BATCHABLE_TOOLS:
                        eff_mergeable += _cnt - 1
                _merge_hinted: set = set()

                # 依赖感知分批：只读一批并行，写工具按资源键分桶、同键必分属不同批。
                # 不能用「有冲突就整批串行」的粗粒度开关——一轮里只要有 1 个 shell_run
                # （跑测试/构建，影响面未知 → 独占键），本可并行的只读读取会被一起拖成
                # 串行。大型任务里这种混合轮很常见，5 个只读白等 5 倍时间。
                # 分批后：只读始终并行、写操作仍严格串行——正确性不变，等待时间大减。
                batches = _plan_batches(pending) if parallel else None
                if parallel and not batches:
                    parallel = False
                # 批次计数：与工具数相比即本轮实际并行度（1 批 5 个 = 真并行；
                # 5 批 5 个 = 全串行）。这是判断提示词有没有让模型愿并发的事实依据。
                if batches:
                    eff_batches += len(batches)
                elif pending:
                    eff_batches += len(pending)

                if parallel:
                    for batch in batches:
                        if len(batch) == 1:
                            # 独占项（写操作/影响面未知）：单独跑，不与任何东西并行
                            i, tn, args = batch[0]
                            async for ev in self._supervised_tool_stream(tn, args):
                                if isinstance(ev, ToolCallProgress):
                                    yield ev
                                else:
                                    outcomes[i] = ev
                            continue
                        # 批内并行：每个工具一个任务，心跳事件汇入缓冲由主循环统一转发
                        progress_buf: list = []

                        def _make_runner(tn: str, args: dict, i: int):
                            async def _run():
                                async for ev in self._supervised_tool_stream(tn, args):
                                    if isinstance(ev, ToolCallProgress):
                                        progress_buf.append(ev)
                                    else:
                                        return i, ev
                            return _run()

                        tasks = [asyncio.create_task(_make_runner(tn, args, i))
                                 for i, tn, args in batch]
                        heartbeat = self._tool_exec_config()["heartbeat"]
                        try:
                            while tasks:
                                done, rest = await asyncio.wait(tasks, timeout=heartbeat)
                                while progress_buf:
                                    yield progress_buf.pop(0)
                                for t in done:
                                    i, ev = t.result()
                                    outcomes[i] = ev
                                tasks = list(rest)
                        finally:
                            for t in tasks:
                                if not t.done():
                                    t.cancel()
                else:
                    # 顺序执行（同样带心跳），保持原有串行语义
                    for i, tn, args in pending:
                        async for ev in self._supervised_tool_stream(tn, args):
                            if isinstance(ev, ToolCallProgress):
                                yield ev
                            else:
                                outcomes[i] = ev

                ordered_outcomes = [outcomes.get(i, ("工具执行结果丢失", False))
                                    for i in range(len(prepared))]

                for (tc, tool_name, tool_args_str, arguments, _err), (result, success) in zip(prepared, ordered_outcomes):
                    # 截断过长结果（代码/技能类工具放宽上限；特殊 JSON 由 server 完整消费）
                    _raw_result = result
                    result = _fit_tool_result(result, tool_name)
                    if len(result) < len(str(_raw_result)):
                        eff_truncations += 1

                    # 渐进式披露：skill_help 读完说明书后，把该技能的工具按需注册为可调用
                    if tool_name == "skill_help" and isinstance(arguments, dict):
                        _skill = str(arguments.get("skill_name") or "").strip()
                        _added = self._activate_skill(_skill)
                        if _added:
                            result = (result or "") + (
                                f"\n\n（已按需加载技能 {_skill} 的 {_added} 个工具，"
                                "现在可以直接调用）")
                            # 同步刷新下一轮 LLM 请求的工具列表（含白名单过滤）
                            if allowed_tools:
                                tools = [t for t in self._all_tools
                                         if t.get("function", {}).get("name") in allowed_set]
                            else:
                                tools = self._all_tools

                    yield ToolCallResult(tool_name=tool_name, result=result, success=success)

                    eff_tool_calls += 1
                    if tool_name not in eff_call_names:
                        eff_call_names.append(tool_name)
                    if not success:
                        eff_tool_errors += 1
                        eff_err_names.append(tool_name)
                    _kinds = _rule_op_kinds(tool_name, arguments)
                    eff_img_ops += _kinds[0]
                    eff_music_ops += _kinds[1]
                    eff_delete_ops += _kinds[2]
                    _fp = _tool_fp(tool_name, arguments)
                    if _fp in eff_seen:
                        eff_re_reads += 1
                        # 只给 LLM 那份加提示：UI 与记忆库存原样结果
                        _llm_result = str(result) + REPEAT_CALL_HINT
                    else:
                        eff_seen.add(_fp)
                        # 本轮首次出现，但在更早的轮次里出现过 → 跨轮重复（只统计）
                        if _fp in self._cross_round_fps:
                            eff_cross_reads += 1
                        _llm_result = result
                        # 合并提示：本轮同一工具被拆成多次调用、且没用批量参数时，
                        # 基于刚发生的真实行为给一次反馈（每个工具只提示一次，避免啰嗦）。
                        # 已用了批量参数就不再提——否则等于在奖励已经做对的行为上加噪声。
                        _cnt = _tool_name_counts.get(tool_name, 0)
                        _binfo = _BATCHABLE_TOOLS.get(tool_name)
                        if (_binfo and _cnt >= 2 and tool_name not in _merge_hinted
                                and not (isinstance(arguments, dict)
                                         and _binfo[0] in arguments)):
                            _merge_hinted.add(tool_name)
                            _llm_result = str(result) + (
                                f"\n\n【合并提示】你本轮把 {tool_name} 调了 {_cnt} 次。"
                                f"{_binfo[1]} —— 一次调用就能做完，省 {_cnt - 1} 次往返。")
                    _ro_hint = self._single_ro_hint()
                    if _ro_hint:
                        _llm_result = str(_llm_result) + _ro_hint
                        eff_single_ro += 1
                    self._remember_cross_fp(_fp)

                    tool_call_results.append({
                        "name": tool_name,
                        "arguments": arguments,
                        "result": result,
                        "llm_result": _llm_result,
                        "success": success,
                    })
                    tool_calls_for_message.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": tool_args_str,
                        },
                    })

            # 回填本轮结果指纹：下一轮的「原地打转」判据要用
            # （结果在变 = 合理轮询不拦；一字不差重复 = 4 轮即停）
            self._note_round_results([tr.get("result") for tr in tool_call_results])

            # 将助手消息和工具结果添加到消息列表
            _img_ok = _img_injectable()
            if text_tool_call:
                if not resume_round:
                    # 断点续跑轮：assistant 消息已包含在恢复的 messages 中
                    pending_chars += len(assistant_content or "")
                    _msg = {
                        "role": "assistant",
                        "content": assistant_content or "",
                    }
                    if round_reasoning and _needs_reasoning_echo(config.get("model")):
                        _msg["reasoning_content"] = round_reasoning
                        # reasoning 回传同样进 prompt，漏计会让上下文预算少算一大截
                        pending_chars += len(round_reasoning)
                    messages.append(_msg)
                for tr in tool_call_results:
                    _res_text = tr.get("llm_result") or tr["result"]
                    _rc = len(str(_res_text))
                    tool_chars += _rc
                    # 前缀也是 prompt 的一部分：先拼 content 再取长度，改前缀不会再漏
                    _content = f"【工具 {tr['name']} 已执行】结果：{_res_text}"
                    pending_chars += len(_content)
                    messages.append({
                        "role": "system",
                        # llm_result 可能带「重复调用」提示；记忆库与前端仍用原样结果
                        "content": _content,
                    })
                    pending_chars += _append_img_messages(
                        messages, _img_marks(_res_text), tr.get("name") or "", _img_ok)
            else:
                if not resume_round:
                    pending_chars += (len(assistant_content or "")
                                      + len(str(tool_calls_for_message)))
                    _msg = {
                        "role": "assistant",
                        "content": assistant_content or None,
                        "tool_calls": tool_calls_for_message,
                    }
                    if round_reasoning and _needs_reasoning_echo(config.get("model")):
                        _msg["reasoning_content"] = round_reasoning
                        # reasoning 回传同样进 prompt，漏计会让上下文预算少算一大截
                        pending_chars += len(round_reasoning)
                    messages.append(_msg)
                for tr, _tc in zip(tool_call_results, tool_calls_for_message):
                    _res_text = tr.get("llm_result") or tr["result"]
                    _rc = len(str(_res_text))
                    tool_chars += _rc
                    pending_chars += _rc
                    messages.append({
                        "role": "tool",
                        "tool_call_id": _tc["id"],
                        # llm_result 可能带「重复调用」提示；记忆库与前端仍用原样结果
                        "content": _res_text,
                    })
                    # 带 [[IMG:path]] 的结果 → 紧跟一条多模态 user 消息，模型直接看像素
                    pending_chars += _append_img_messages(
                        messages, _img_marks(_res_text), tr.get("name") or "", _img_ok)

            # 轮内工具历史压缩：本轮结果已入 messages，先压缩再落断点/进入下一轮，
            # 保证断点文件与后续 LLM 请求的上下文都保持有界
            try:
                _compact_tool_history(messages)
            except Exception:
                pass

            # 保存到记忆（断点续跑轮：先清掉中断轮可能已半写入的残留，保证幂等；
            # 再检查断点是否仍是本轮——用户若已发新指令，则放弃续跑，防止误删）
            if resume_round:
                if not _turn_ckpt_still_mine(turn_user, turn_id or ""):
                    logger.warning("对话轮断点已被新对话覆盖，放弃续跑本轮")
                    return
                anchor = int(resume_ckpt.get("memory_anchor") or 0)
                if anchor > 0:
                    try:
                        # 校验与删除放进同一临界区：并发新对话轮先拿到断点锁时，
                        # guard 会失败，避免误删新轮刚写入的消息
                        ok = await self.memory.delete_messages_after_if(
                            anchor,
                            guard=lambda: _turn_ckpt_still_mine(turn_user, turn_id or ""),
                        )
                        if not ok:
                            logger.warning("对话轮断点已被新对话覆盖，放弃续跑本轮")
                            return
                    except Exception as e:
                        logger.warning(f"清理中断轮残留消息失败: {e}")
                resume_round = False
                resume_pending.clear()

            display_text = assistant_content or ""
            if text_tool_call or has_tool_calls:
                # 静默执行模式：中间过程话不落库、不进入摘要与历史，
                # 只保留每轮最终结论（assistant_content 仍完整进 API 对话）
                display_text = ""
            await self.memory.add_message(
                "assistant", display_text,
                tool_calls=tool_calls_for_message,
            )
            for tr in tool_call_results:
                await self.memory.add_message(
                    "tool", tr["result"],
                )

            # 断点落盘（c：本轮工具已执行完并写入记忆，下一步继续 LLM）
            try:
                ckpt_memory_anchor = await self.memory.get_max_message_id()
            except Exception:
                pass
            await _save_round_ckpt([], "", ckpt_memory_anchor,
                                   ckpt_round_num=tool_round + 1)

            full_text = display_text

        # 超过最大轮数 / 死循环保护触发
        if loop_break:
            # 工具轮正文为空时只给简短说明，绝不把累积的推理步骤拼进正文
            if not full_text:
                full_text = (
                    f"（连续 {_repeat_guard_limit()} 轮调用完全相同的工具和参数，已自动停止；"
                    "建议换一种方法或拆小任务再试）")
            yield TextDelta(full_text)
            await self.memory.add_message("assistant", full_text, source=msg_source)
        elif full_text:
            # 正常完成：正文即结论，不再追加「说继续」的续跑提示——
            # 那句话把每次正常交付都读成「做了一半」，把判断成本推回给用户
            await self.memory.add_message("assistant", full_text)
        else:
            # 无正文 = 轮数用尽或输出被截断：先自动补一次「无工具收尾」，把已有
            # 进展写成结论交付。连收尾都拿不到正文，才如实说明中断原因。
            _closing = await self._closing_summary(messages, config, mode)
            if _closing:
                full_text = _closing
                yield TextDelta(_closing)
                await self.memory.add_message("assistant", _closing)
            elif reasoning_all:
                _tail = "（本轮输出被截断，未产出正文；已完成的工具结果均已落盘）"
                yield TextDelta(_tail)
                await self.memory.add_message("assistant", _tail)
            else:
                yield TextDelta("（本轮没有产出内容，任务可能未完成）")

        # 方法末尾兜底：即使没走"对话结束"分支也尽量发出用量事件
        if not usage_emitted and sum_total > 0:
            usage_emitted = True
            yield UsageEvent(prompt_tokens=sum_prompt,
                             completion_tokens=sum_completion,
                             total_tokens=sum_total, rounds=rounds,
                             context_window=context_window,
                             cache_hit_tokens=sum_cache_hit,
                             cache_miss_tokens=sum_cache_miss,
                             context_tokens=last_prompt)
        await _record_stats()
        _record_turn_metrics()

    # ==================== 游戏模式 ====================

    async def chat_stream_simple(self, message: str, history: list = None) -> AsyncIterator[str]:
        """简化的流式对话接口 —— 只产出文本，兼容 web_agent.chat_stream_async。

        工具调用在后台执行，用户只看到最终文本结果。
        """
        async for event in self.chat_stream(message, history=history):
            if isinstance(event, TextDelta):
                yield event.text

    # ==================== 非流式接口 ====================

    async def chat(self, message: str, history: list = None) -> str:
        """非流式对话，返回完整回复。"""
        text = ""
        async for event in self.chat_stream(message, history=history):
            if isinstance(event, TextDelta):
                text += event.text
        return text.strip()

    # ==================== 多角色台词生成（游戏用） ====================

    async def generate_character_line(
        self,
        system_prompt: str,
        context: str,
        max_tokens: int = 220,
        temperature: float = 0.85,
    ) -> Optional[str]:
        """为某个角色（HR/求职者/员工）生成一句台词。

        纯 LLM 直呼接口：只根据传入的角色人设(system_prompt)与当前语境(context)
        生成一句口语化台词，不读写全局记忆、不触发工具链、不改变 Agent 状态，
        供赛博公司等多人游戏中的多角色对话使用。失败返回 None，由调用方回退脚本。

        Args:
            system_prompt: 角色人设（角色卡片的 system_prompt + 游戏角色指令）
            context: 当前对话语境（面试记录 / 需要回应的话）
            max_tokens: 生成上限
            temperature: 随机性
        """
        await self._ensure_initialized()
        cfg = self._config
        try:
            resp = await self._retry_create(
                kind="character_line",
                model=cfg.get("model", ""),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=0.9,
            )
            text = (resp.choices[0].message.content or "").strip()
            # 去掉可能包裹的引号 / 角色名冒号 / 多余换行，保持口语化短句
            text = re.sub(r"^【[^】]*】\s*", "", text)
            text = text.strip('"“”‘’\'').strip()
            text = re.sub(r"\s*\n\s*", " ", text).strip()
            if not text:
                return None
            # 截断到合理长度（防止模型话痨）
            return text[:max_tokens * 2]
        except Exception as e:
            logger.warning(f"generate_character_line 失败: {e}")
            return None

    # ==================== 记忆管理 ====================

    async def get_history(self) -> list:
        """获取当前会话的历史记录。"""
        await self._ensure_initialized()
        return await self.memory.get_history_for_llm()

    async def get_sessions(self, limit: int = 50, query: str = None,
                           include_archived: bool = False) -> list:
        """获取用户的所有会话列表（含消息数/摘要/token 估算/置顶/归档；支持搜索）。

        Args:
            limit: 返回条数上限
            query: 非空时按标题或消息内容模糊搜索
            include_archived: True 时包含已归档会话
        """
        await self._ensure_initialized()
        return await self.memory.list_sessions(
            limit=limit, query=query, include_archived=include_archived)

    async def switch_session(self, session_id: str):
        """切换到指定会话（完整继承：消息上下文/摘要按 session_id 自动跟随）。

        同时重置滞回历史窗口，避免跨会话锚点误配导致缓存串上下文。
        """
        await self._ensure_initialized()
        self._hist_view = None  # 滞回窗口重建：切换会话后按新会话历史重新定位
        await self.memory.set_session_id(session_id)
        return self.memory.session_id

    async def rename_session(self, session_id: str, title: str = "") -> bool:
        """重命名指定会话（任何历史会话，不限当前）。"""
        if not session_id or not title:
            return False
        await self._ensure_initialized()
        await self.memory.update_title(title.strip()[:60], session_id=session_id)
        return True

    async def set_session_pinned(self, session_id: str, pinned: bool = True):
        """置顶 / 取消置顶指定会话。"""
        if not session_id:
            return
        await self._ensure_initialized()
        await self.memory.set_session_pinned(session_id, pinned)

    async def set_session_archived(self, session_id: str, archived: bool = True):
        """归档 / 取消归档指定会话。"""
        if not session_id:
            return
        await self._ensure_initialized()
        await self.memory.set_session_archived(session_id, archived)

    async def get_session_history(self, session_id: str,
                                  max_rounds: int = None) -> list:
        """获取指定会话的完整历史（user/ai 轮次，回滚继承时给前端渲染用）。"""
        await self._ensure_initialized()
        return await self.memory.get_session_history_pairs(session_id, max_rounds=max_rounds)

    async def get_session_summary(self, session_id: str = None) -> str:
        """获取指定会话（默认当前）的最新摘要文本。"""
        await self._ensure_initialized()
        return await self.memory.get_session_summary(session_id)

    async def close_current_session(self):
        """关闭当前会话。"""
        if self.memory:
            await self.memory.close_session()
        self.reset_session_skills()  # 新会话从基础工具起步，技能激活集不跨会话

    async def delete_session(self, session_id: str):
        """删除指定会话。"""
        if self.memory:
            await self.memory.delete_session(session_id)

    # ==================== 清理 ====================

    async def shutdown(self):
        """关闭 Agent，释放所有资源。"""
        if self.memory:
            await self.memory.close_session()
        runtime = _get_runtime()
        if runtime is not None:
            runtime.unregister_agent(self.user_id)
        self._initialized = False


# ==================== 全局 Agent 实例（单例模式） ====================

_global_agent: Optional[AIAgent] = None
_agent_lock = asyncio.Lock()


async def get_agent(user_id: str = "default") -> AIAgent:
    """获取全局 Agent 实例（延迟初始化）。"""
    global _global_agent
    if _global_agent is None:
        async with _agent_lock:
            if _global_agent is None:
                _global_agent = AIAgent(user_id=user_id)
                await _global_agent.initialize()
    return _global_agent


# ==================== 兼容 web_agent 的接口 ====================

async def chat_stream_async(message: str, history: list) -> AsyncIterator[str]:
    """兼容 web_agent.chat_stream_async 的接口。

    新代码应直接使用 AIAgent。
    """
    agent = await get_agent()
    async for delta in agent.chat_stream_simple(message, history=history):
        yield delta
