"""长期记忆模块 —— SQLite 持久化对话历史、会话管理、记忆检索与摘要。

特性：
- SQLite 本地存储，无需额外数据库服务
- 按 user_id 管理多用户会话
- 自动生成对话摘要（长对话压缩）
- 智能记忆检索：最近 N 条 + 相关历史摘要
- 支持异步操作（线程池执行同步 SQLite）
"""
import asyncio
import functools
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import re

logger = logging.getLogger("memory")

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "chat_memory.db"

# 摘要触发阈值：单会话超过此消息数后自动生成摘要
SUMMARY_THRESHOLD = 20
# 摘要间隔：每隔多少条消息生成一次摘要
SUMMARY_INTERVAL = 10
# 检索时返回的最近消息数
RECENT_MESSAGE_COUNT = 10
# 检索时返回的最大摘要数
MAX_SUMMARIES = 3

# 用户记忆类别（偏好记忆结构化：称呼 / 音乐 / 影片 / 城市 / 其他）
PREFERENCE_CATEGORIES = ("称呼", "音乐", "影片", "城市", "工作", "习惯", "计划", "其他")
# 主动回忆（按需相关性检索）默认参数；可在 settings.json 的 memory 段覆盖
RECALL_ENABLED = True
RECALL_TOP_K = 6
RECALL_MAX_CHARS = 800
# 相关回忆注入的最大估算 token（与 recall_max_chars 取更小者）
RECALL_MAX_TOKENS = 200

# 上下文打包（token 预算）默认参数；可在 settings.json 的 memory 段覆盖
# 提示词预算 = context_window * token_budget_ratio（max_prompt_tokens > 0 时优先用固定值）
CONTEXT_TOKEN_BUDGET_RATIO = 0.5
MAX_PROMPT_TOKENS = 0
# 记忆/摘要/近期消息在提示词预算中的分配比例（留给历史的剩余部分）
MEMORY_BLOCK_RATIO = 0.35
# 归档：0=不删除任何原始消息；>0 时定期删除「已关闭会话」中超过 N 天的原始消息
# （摘要与长期记忆保留，检索不依赖已归档消息）。默认关闭，避免误删用户数据。
ARCHIVE_KEEP_DAYS = 0
ARCHIVE_MIN_INTERVAL_SEC = 6 * 3600
# 长期记忆淘汰：超过 memory_evict_days 天、且按时间衰减后 importance 低于阈值、
# 且长时间未被访问的记忆会被删除（仅当前命名空间；摘要与近期记忆不受影响）。
MEMORY_EVICT_DAYS = 180
MEMORY_EVICT_IMPORTANCE = 0.45
MEMORY_EVICT_MIN_ACCESS_DAYS = 90
# 时间衰减半衰期（天）：召回检索与长期记忆选择时，越久没被提到的内容权重越低
RECALL_RECENCY_HALF_LIFE_DAYS = 14.0
MEMORY_DECAY_HALF_LIFE_DAYS = 90.0
# 长期记忆近重复合并的相似度阈值（2-gram Jaccard 加权）
MEMORY_MERGE_SIMILARITY = 0.62
# 长期记忆卫生节流：去重/淘汰/封顶每 N 秒最多跑一次（写路径热，读路径零成本）
MEMORY_PRUNE_INTERVAL_SEC = 300
# 每个命名空间长期记忆总量封顶（超出后按「有效重要度」淘汰最不重要的）
MEMORY_CAP_PER_NAMESPACE = 120

# 用于类别推断与关键词降级提取的常见模式（类别 -> 触发词）
_CATEGORY_KEYWORDS = {
    "音乐": ("歌", "音乐", "曲", "歌手", "唱", "听", "乐队", "歌单", "周杰伦", "毛不易", "邓紫棋",
             "流行", "摇滚", "民谣", "古典", "钢琴", "曲子"),
    "影片": ("电影", "影片", "剧", "番", "视频", "b站", "bilibili", "纪录片", "动画", "综艺", "追剧", "片单"),
    "城市": ("住在", "城市", "老家", "家乡", "广州", "深圳", "上海", "北京", "杭州", "成都", "武汉",
             "南京", "苏州", "西安", "重庆", "长沙", "厦门", "青岛"),
    "称呼": ("称呼", "叫我", "喊我", "叫我主人", "昵称", "叫我宝宝", "叫我亲爱的"),
    "工作": ("工作", "上班", "公司", "同事", "老板", "项目", "代码", "编程", "写代码", "加班", "出差",
             "创业", "职业", "行业", "岗位", "升职", "跳槽"),
    "习惯": ("习惯", "经常", "总是", "每天", "每周", "作息", "早起", "熬夜", "健身", "跑步", "运动",
             "阅读", "看书", "打游戏", "玩游戏", "刷剧"),
    "计划": ("计划", "打算", "准备", "决定", "约定", "约好", "周末", "明天", "下周", "下个月",
             "放假", "旅行", "旅游", "去", "学", "考", "报名", "安排"),
}

# ==================== 分层记忆上下文打包（token 预算） ====================
# 目标：短期窗口（最近 N 轮）+ 长期摘要 + 长期关键信息 + 检索召回，
# 每层都受 token 预算约束；context_stats 表记录每轮 raw/packed/actual 用量，
# 用于量化 token 节省（按 context_stats 表的实际用量核算）。
# 总开关：False 时各打包函数回退为旧行为（不设 token 上限），保证可一键回滚。
HIERARCHICAL_PACKING = True
# 2026-08-29 工程上下文增强：此前预算过小（短期 1200 token ≈ 2 轮、摘要 200 token
# ≈ 1 条 120 字），长工具任务里"刚做完的事下一轮就丢"，只能靠反复重读文件找回，
# 实测单任务打到 60~107 轮工具调用。现按 128k 上下文窗口上调到能装下
# 「最近 5~8 轮 + 自包含摘要 + 最近真实工具结果」的规模；仍远小于提示词预算。
SHORT_TERM_MAX_TOKENS = 8000             # 短期窗口（最近轮次）预算
SHORT_TERM_MAX_CHARS_PER_ROUND = 1200    # 单轮历史最大字符（超出截断加 …）
# 2026-08-31 修复「前两轮就忘」：短期窗口对最近 N 个用户轮「保底保留」——
# token 预算只约束更旧轮，保证最近几轮对话绝不被工具长文挤掉。
SHORT_TERM_MIN_ROUNDS = 6                # 短期窗口保底轮数（最近用户轮必保留）
# 单条工具结果在短期窗口的最大字符：工具轮单条结果常达上万字（整轮几千 token），
# 若全部保留，3000 token 预算只装得下 2~3 轮。截断后仍保留要点（完整结果在库中可召回）。
SHORT_TERM_MAX_CHARS_PER_TOOL = 500
# 2026-09-11 实测（跨轮前缀探针）：短期窗口实际产出 200 条消息 / 正文 5 万字符，
# 而**跨轮首次调用命中只剩 1.6k token**（整条前缀每轮全废，冷启动 ≈ 55k token/轮，
# 占单轮成本 37%）。三个原因，都在「预算没算全」：
#   ① token 统计只算 content，tool_calls 的 arguments JSON 完全不进预算——
#      一次 shell_run 的命令动辄上千字符，一轮 60 次调用就是几万字符，预算名存实亡；
#   ② 单轮字符上限只管 user/assistant 正文，一轮 60 条工具消息依然无限长；
#   ③ 「保底轮」ignore_budget=True 直接跳过预算检查。
# 对应三处修正：统计含 tool_calls；参数也截断；单轮只留最近 N 次工具交互（成对保留）。
SHORT_TERM_MAX_CHARS_PER_TOOL_CALL = 300  # 单条工具调用参数（arguments）最大字符
SHORT_TERM_KEEP_LAST_TOOLS = 3           # 单轮保留最近 N 次工具交互（其余成对丢弃）
SUMMARY_MAX_TOKENS = 800                 # 长期摘要块预算（累计式摘要自包含，最新一条优先全量）
SUMMARY_MAX_CHARS_PER_ITEM = 600         # 单条摘要最大字符（与累计摘要 600 字上限一致）
LONG_TERM_MAX_TOKENS = 300               # 常驻长期记忆块预算
LONG_TERM_TOP_K = 4                      # 常驻长期记忆条数（稳定排序）
RECALL_MAX_TOKENS = 400                  # 主动回忆预算（与 recall_max_chars 取更小者）
# 最近任务执行摘要：每轮注入最近一次带工具执行的用户轮次的真实工具结果上限
RECENT_WORK_MAX_CHARS = 1200
RECORD_CONTEXT_STATS = True              # 每轮写入 context_stats

# 用于类别推断与关键词降级提取的常见模式（类别 -> 触发词）
_CATEGORY_KEYWORDS = {
    "音乐": ("歌", "音乐", "曲", "歌手", "唱", "听", "乐队", "歌单", "周杰伦", "毛不易", "邓紫棋",
             "流行", "摇滚", "民谣", "古典", "钢琴", "曲子"),
    "影片": ("电影", "影片", "剧", "番", "视频", "b站", "bilibili", "纪录片", "动画", "综艺", "追剧", "片单"),
    "城市": ("住在", "城市", "老家", "家乡", "广州", "深圳", "上海", "北京", "杭州", "成都", "武汉",
             "南京", "苏州", "西安", "重庆", "长沙", "厦门", "青岛"),
    "称呼": ("称呼", "叫我", "喊我", "叫我主人", "昵称", "叫我宝宝", "叫我亲爱的"),
    "工作": ("工作", "上班", "公司", "同事", "老板", "项目", "代码", "编程", "写代码", "加班", "出差",
             "创业", "职业", "行业", "岗位", "升职", "跳槽"),
    "习惯": ("习惯", "经常", "总是", "每天", "每周", "作息", "早起", "熬夜", "健身", "跑步", "运动",
             "阅读", "看书", "打游戏", "玩游戏", "刷剧"),
    "计划": ("计划", "打算", "准备", "决定", "约定", "约好", "周末", "明天", "下周", "下个月",
             "放假", "旅行", "旅游", "去", "学", "考", "报名", "安排"),
}

_CATEGORY_PATTERN = "|".join(re.escape(c) for c in PREFERENCE_CATEGORIES)

_db_lock = threading.Lock()
_db_local = threading.local()
_last_archive_ts = 0.0  # 归档节流：模块级最近一次归档时间


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def estimate_tokens(text: str) -> int:
    """轻量 token 估算（无依赖、无网络，用于预算控制与节省统计）。

    中文按 ~1 token/字、英文/数字按 4 字符/token、标点按 2 字符/token 估算，
    并加 10% 安全余量，避免预算被低估。真实 token 数以提供方 usage 为准，
    两者之间的误差会通过 context_stats 表持续校准（见 record_context_stats）。

    这是热路径函数（每轮工具循环都要对全部消息算一遍），所以正则预编译在模块级：
    原来每次调用都做一遍函数内 import + 5 次未预编译的全文扫描。
    再做两处纯计数优化（对照 tools/token_est_bench.py，实测 1.66×）：
      · CJK 均为单字符 → 长度差即个数，省掉 findall 构造整个匹配 list；
      · 非空白字符数用 str.split() 的 C 实现求和，省掉再跑一遍空白正则替换。
    注意：估算公式一字未改，数值与历史校准数据完全一致（66 组语料 + 空白边界样本 0 差异）。
    """
    if not text:
        return 0
    s = str(text)
    rest = _CJK_RE.sub("", s)
    cjk = len(s) - len(rest)
    words = _WORD_RE.findall(rest)
    rest_no_words = _WORD_RE.sub("", rest)
    punct = sum(map(len, rest_no_words.split()))
    est = cjk + sum((len(w) + 3) // 4 for w in words) + (punct + 1) // 2
    return int(est * 1.1) + 1


def _clean_think_markers(text: str) -> str:
    """剔除模型输出里泄漏的 think 标记（如 `无</think>无`、<think>…</think>）。"""
    if not text:
        return text
    s = re.sub(r"</?think[^>]*>", "", str(text), flags=re.IGNORECASE)
    s = re.sub(r"^\s*(?:思考|推理|reasoning)\s*[:：]\s*", "", s)
    return s.strip()


# 记忆提取的噪音兜底：模型可能回复"无/同上"或空壳内容，一律不入库
_MEMORY_NOISE = {
    "无", "暂无", "同上", "没有", "无信息", "无新内容", "无新信息",
    "没有新内容", "没有新信息", "没有透露", "未透露", "无。", "暂无。",
}


def _is_memory_noise(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t in _MEMORY_NOISE:
        return True
    # "无无"、"无无无"、"同上同" 等思维链泄漏残留
    if re.fullmatch(r"[无没同上暂无]{1,8}", t):
        return True
    # 纯标记残留（如 "无</think>无" 清洗后只剩"无无"）或长度过短，无信息量
    if len(t) < 4 and not any(ch.isalnum() for ch in t):
        return True
    return False


_SUMMARY_NOISE_RE = re.compile(
    r"^\s*(?:\$ |[✅✔✘⚠🔍⛔]\s|\[exit=|```|--- 旧|\+\+\+ 新|"
    r"index |diff --git |\d{4}-\d{2}-\d{2} |Traceback|Error code:)",
    re.M,
)


def _clean_summary_noise(text: str) -> str:
    """清洗摘要文本里的工具输出噪音（历史遗留的关键词降级摘要会混入原始命令/日志）。"""
    if not text:
        return text
    lines = [ln for ln in str(text).split("\n") if ln.strip()]
    kept = [ln for ln in lines if not _SUMMARY_NOISE_RE.match(ln)]
    return "\n".join(kept).strip() or "(摘要内容缺失)"


def _recency_factor(ts: float, half_life_days: float) -> float:
    """按距今天数计算指数衰减系数：half_life 天后权重减半。"""
    age_days = max(0.0, (time.time() - float(ts or 0)) / 86400.0)
    return 2.0 ** (-age_days / max(1.0, half_life_days))


# ==================== 分层记忆打包：纯函数（token 预算） ====================
# 设计要点：
# - 每层独立预算（short_term/summary/long_term/recall），调用方传入「新→旧」文本，
#   从最新开始填入预算，超预算即停——保证"最近信息优先保留"。
# - 层内按字符上限提前截断（如单轮历史 500 字符），避免一条超长消息独占整层预算。
# - estimate_tokens 估算 token；真实用量由 context_stats 表持续校准。

def _truncate_chars(text: str, max_chars: int) -> str:
    """按字符上限截断并追加省略号。"""
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + '…'


def _pack_by_token_budget(texts: list, budget_tokens: int,
                          max_chars_per_item: int = 0) -> tuple:
    """按 token 预算挑选文本（输入新→旧，输出保持原序），至少保留第一条。

    Returns:
        (kept, tokens) kept=满足预算的文本列表（新→旧），tokens=实际估算 token。
    """
    kept, used = [], 0
    for text in texts:
        text = _truncate_chars(text, max_chars_per_item) if max_chars_per_item else text
        if not text or not text.strip():
            continue
        t = estimate_tokens(text)
        if kept and used + t > budget_tokens:
            break
        if not kept and used + t > budget_tokens:
            # 第一条就超预算：硬截断到预算可容纳的字符（中文约 1 token/字）
            text = _truncate_chars(text, max(16, budget_tokens))
            t = estimate_tokens(text)
        kept.append(text)
        used += t
    return kept, used


def _estimate_record_tokens(m: dict) -> int:
    """估算单条历史记录的 token：正文 + tool_calls + tool_results 全算。

    以前只算 content —— 于是 tool_calls 的 arguments JSON 完全不进预算，
    预算形同虚设，短期窗口能涨到几万 token（实测 200 条消息 / 5 万字符正文）。
    预算只要算不全，它就不是预算。
    """
    t = estimate_tokens(m.get("content") or "")
    for key in ("tool_calls", "tool_results"):
        v = m.get(key)
        if not v:
            continue
        try:
            t += estimate_tokens(json.dumps(v, ensure_ascii=False))
        except Exception:
            t += estimate_tokens(str(v))
    return t


def _cap_tool_calls(tool_calls, max_chars: int):
    """截断工具调用里 arguments 文本（id/type/function.name 结构一律不动）。

    只截文本不动结构：function-calling 历史仍然合法（提供方不会 400），
    旧调用的参数细节被压缩——它们对下一轮几乎没有价值。
    """
    if not max_chars or not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn = tc.get("function")
        if isinstance(fn, dict):
            args = fn.get("arguments")
            if isinstance(args, str) and len(args) > max_chars:
                fn = dict(fn)
                fn["arguments"] = args[:max_chars] + "…"
                tc = dict(tc)
                tc["function"] = fn
        out.append(tc)
    return out


def _limit_round_tools(rnd: list, keep_last: int) -> list:
    """一轮里只保留最近 keep_last 次工具交互，更早的成对丢弃。

    为什么必须成对：assistant 的 tool_calls 与紧随其后的 tool 响应必须配对，
    丢一半会让 function-calling 历史出现悬空引用（提供方直接 400）。
    带正文的 assistant 消息降级为纯文本消息保留——结论比调用细节值钱。
    """
    if not keep_last or keep_last <= 0:
        return rnd
    idx = [i for i, m in enumerate(rnd)
           if m.get("role") == "assistant" and m.get("tool_calls")]
    if len(idx) <= keep_last:
        return rnd
    keep_from = idx[len(idx) - keep_last]
    out = []
    for i, m in enumerate(rnd):
        if i >= keep_from:
            out.append(m)
            continue
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            if (m.get("content") or "").strip():
                out.append({k: v for k, v in m.items() if k != "tool_calls"})
            continue
        out.append(m)
    return out


def _drop_dangling_tail(packed: list) -> list:
    """去掉尾部「只有 tool_calls 没有配对响应」的 assistant 消息。

    截断可能正好发生在一次工具交互中间：留下一个没有响应的 tool_calls
    会让提供方 400。宁可丢掉这一条，也不能发出非法请求。
    """
    while packed:
        last = packed[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            packed = packed[:-1]
            continue
        break
    return packed


def _pack_history_records(records: list, budget_tokens: int,
                          max_chars_per_round: int = 0,
                          min_rounds: int = 0,
                          max_chars_per_tool: int = 0,
                          max_chars_per_tool_call: int = 0,
                          keep_last_tools: int = 0) -> list:
    """按「轮」打包短期窗口历史（新→旧挑选，输出恢复旧→新）。

    records: 旧→新的消息列表 [{"role","content", tool_calls?, tool_results?}...]。
    先按轮分组（user 与其后的 assistant/tool 消息归为一轮，不拆散问答对）：
    - 最新一轮整体保留：不受单轮字符上限截断，超 token 预算时才按预算硬截断，
      保证「刚发生的事」完整进入下一轮上下文（修复长工具轮把上一轮挤掉的问题）；
    - min_rounds>0：最近 N 个「用户轮」保底保留（只受字符上限约束，不因预算丢整轮），
      保证最近几轮对话绝不被工具长文挤掉（修复"前两轮就忘"）；
    - 更旧轮按剩余预算由新到旧挑选，放不下的整轮跳过。
    tool_calls/tool_results 结构原样保留（避免截断工具调用 JSON 导致
    function-calling 历史失效）；但参数文本与工具交互条数受两个上限约束——
    它们是历史里最贵的隐形大头（见 SHORT_TERM_MAX_CHARS_PER_TOOL_CALL 处的实测）。
    """
    if not records:
        return []
    rounds = _group_history_rounds(records)
    kept, used = [], 0
    user_rounds_kept = 0
    for idx, rnd in enumerate(reversed(rounds)):  # 新→旧
        is_newest = idx == 0
        has_user = any(m.get("role") == "user" for m in rnd)
        guaranteed = min_rounds > 0 and user_rounds_kept < min_rounds
        # 最新一轮不截字符（超预算才硬截断）；旧轮仍按单轮上限截断
        cap = 0 if is_newest else max_chars_per_round
        packed, rnd_tokens = _pack_one_round(
            rnd, cap, budget_tokens, is_newest,
            max_chars_per_tool=max_chars_per_tool,
            ignore_budget=guaranteed,
            max_chars_per_tool_call=max_chars_per_tool_call,
            keep_last_tools=keep_last_tools)
        packed = _drop_dangling_tail(packed)
        if not packed:
            continue
        if not guaranteed and used + rnd_tokens > budget_tokens:
            continue  # 旧轮放不下：整轮跳过，不拆散一问一答
        kept = packed + kept
        used += rnd_tokens
        if has_user:
            user_rounds_kept += 1
    return kept


def _group_history_rounds(records: list) -> list:
    """把消息列表（旧→新）按轮分组。

    user 消息开始新一轮，其后同轮的 assistant/tool 消息归入该轮；
    无 user 的 assistant 消息（AI 主动发言）独立成轮。
    """
    rounds = []
    current = None
    for m in records:
        if m.get("role") == "user":
            current = [m]
            rounds.append(current)
        elif current is not None:
            current.append(m)
        else:
            current = [m]
            rounds.append(current)
    return rounds


def _pack_one_round(rnd: list, cap: int, budget_tokens: int,
                    is_newest: bool, max_chars_per_tool: int = 0,
                    ignore_budget: bool = False,
                    max_chars_per_tool_call: int = 0,
                    keep_last_tools: int = 0) -> tuple:
    """打包单轮消息，返回 (packed, tokens)。

    cap>0 时截断 user/assistant 纯文本；max_chars_per_tool>0 时截断 tool 结果
    文本（tool_calls/tool_results JSON 结构不动，function-calling 历史仍有效）；
    max_chars_per_tool_call>0 时截断工具调用参数；keep_last_tools>0 时只保留
    最近 N 次工具交互（成对，不产生悬空引用）。
    ignore_budget=True（保底轮）时不因预算丢消息，只按字符上限截断；
    最新一轮单条超预算时按剩余预算硬截断，旧轮超预算即停（由调用方决定整轮跳过）。
    """
    if keep_last_tools:
        rnd = _limit_round_tools(rnd, keep_last_tools)
    packed, used = [], 0
    for m in rnd:
        content = m.get('content') or ''
        if m.get('role') in ('user', 'assistant') and cap:
            content = _truncate_chars(content, cap)
        elif m.get('role') == 'tool' and max_chars_per_tool:
            content = _truncate_chars(content, max_chars_per_tool)
        out = {**m, 'content': content}
        if max_chars_per_tool_call and out.get('tool_calls'):
            out['tool_calls'] = _cap_tool_calls(out['tool_calls'],
                                               max_chars_per_tool_call)
        # 预算必须按「整条记录」计（含 tool_calls JSON），否则预算就是摆设
        t = _estimate_record_tokens(out)
        if used + t > budget_tokens:
            if ignore_budget:
                # 保底轮：预算不足也不丢内容，只按字符上限截断
                packed.append(out)
                used += t
                continue
            if is_newest and content and used < budget_tokens:
                # 最新轮：截断本条到剩余预算，尽量保住问答内容
                remain = max(16, budget_tokens - used)
                content = _truncate_chars(content, remain)
                out = {**out, 'content': content}
                t = _estimate_record_tokens(out)
                packed.append(out)
                used += t
            break
        packed.append(out)
        used += t
    return packed, used


def _format_recall_lines(items: list, max_chars: int, token_budget: int = 0) -> tuple:
    """把召回项格式化为「- [来源] 文本」行，受字符上限与 token 预算双重约束。

    Args:
        items: recall_memories 的返回项（已按相关度排序）
        max_chars: 字符上限（默认 recall_max_chars）
        token_budget: token 预算（>0 时与 max_chars 取更严格者，强制生效）

    Returns:
        (lines, items_count)
    """
    seen, lines, used_chars, used_tokens = set(), [], 0, 0
    for it in items:
        text = (it.get('text') or '').strip().replace('\n', ' ')
        if not text or text in seen:
            continue
        seen.add(text)
        tag = {"memory": '长期记忆', "summary": '历史摘要', "message": '此前对话'}.get(it.get('kind'), '记忆')
        line = f'- [{tag}] {text[:80]}'
        t = estimate_tokens(line)
        if lines and used_chars + len(line) > max_chars:
            break
        if lines and token_budget > 0 and used_tokens + t > token_budget:
            break
        if token_budget > 0 and not lines and used_tokens + t > token_budget:
            line = line[:max(16, token_budget)]
            t = estimate_tokens(line)
        lines.append(line)
        used_chars += len(line)
        used_tokens += t
    return lines, len(items)


def _connection_pairs_to_records(pairs: list) -> list:
    """把服务端连接级历史 `[{"user":..,"ai":..}]`（旧→新）转成消息记录列表。

    AI 主动说话轮次以空 user 标记：跳过空 user，
    只保留 assistant 发言，让 LLM 看到「AI 之前主动说过这段话」而不是
    空白的用户消息；用户搭话/插话时可基于此正确回应。
    2026-08-30：同时跳过空 ai 轮次——被打断的轮次没有最终回复，
    保留「用户:xxx / AI:」空对只会让模型以为 AI 什么都没做过。
    """
    records = []
    for h in pairs or []:
        if h.get("user"):
            records.append({'role': 'user', 'content': h.get('user', '')})
        ai = (h.get("ai") or "").strip()
        if ai:
            records.append({'role': 'assistant', 'content': ai})
    return records


def _row_has_tool_calls(row) -> bool:
    """该消息行是否携带非空工具调用（用于识别工具循环里的中间 assistant 轮）。

    兼容 sqlite3.Row 与 dict 两种行对象（Row 无 .get，用 [] 取值）。
    """
    try:
        if hasattr(row, "get"):
            tc = row.get("tool_calls")
        else:
            tc = row["tool_calls"]
    except (KeyError, IndexError, TypeError):
        tc = None
    if not tc:
        return False
    try:
        parsed = json.loads(tc) if isinstance(tc, str) else tc
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(parsed)


def _iter_turn_texts(rows: list) -> list:
    """把消息行（旧→新）规整为 user/ai 轮次列表，返回 [{"user","ai"}, ...]。

    - 跳过工具循环的中间 assistant 轮（带非空 tool_calls）与空消息，
      只保留每轮最终回复，避免恢复历史时把"我先看看…"之类过程话当结论；
    - AI 主动发言（无 user）保留为空白 user 轮次。
    """
    history = []
    pending_user = None
    pending_ai: list = []
    for row in rows:
        role = row.get("role")
        if role == "user":
            if pending_user is not None or pending_ai:
                history.append({"user": pending_user or "", "ai": "\n".join(pending_ai)})
            pending_user = (row.get("content") or "").strip()
            pending_ai = []
        elif role == "assistant":
            if _row_has_tool_calls(row):
                continue  # 中间工具轮：不是最终回复
            content = (row.get("content") or "").strip()
            if not content:
                continue
            if pending_user is not None:
                pending_ai.append(content)
            else:
                # AI 主动说话轮次（无 user）：保留独立轮次
                history.append({"user": "", "ai": content})
    if pending_user is not None:
        history.append({"user": pending_user, "ai": "\n".join(pending_ai)})
    elif pending_ai:
        history.append({"user": "", "ai": "\n".join(pending_ai)})
    return history


# SQLite 锁冲突容忍：连接/写操作/初始化遇 'database is locked' 时的重试次数与间隔
_BUSY_RETRIES = 5
_BUSY_SLEEP = 0.5


def _is_busy_error(e: Exception) -> bool:
    """判断异常是否为 SQLite 锁冲突（database is locked / database table is locked）。"""
    return isinstance(e, sqlite3.OperationalError) and (
        'locked' in str(e).lower() or 'busy' in str(e).lower()
    )


def _is_duplicate_column(e: Exception) -> bool:
    """判断异常是否为 duplicate column name（迁移幂等：字段已存在属预期）。"""
    return isinstance(e, sqlite3.OperationalError) and 'duplicate column' in str(e).lower()


def _connect_db() -> sqlite3.Connection:
    """创建数据库连接并应用 PRAGMA（WAL + busy_timeout）。

    锁冲突时自动重试，不再让 'database is locked' 中断连接建立。
    """
    last_err = None
    for attempt in range(1, _BUSY_RETRIES + 1):
        try:
            conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            if not _is_busy_error(e):
                raise
            logger.warning(f'SQLite 连接/PRAGMA 被锁（第 {attempt}/{_BUSY_RETRIES} 次）: {e}')
            time.sleep(_BUSY_SLEEP)
    raise last_err


def _get_db() -> sqlite3.Connection:
    """获取数据库连接（线程安全 + 连接复用 + 超时等待 + 自动重连）。

    每个线程复用同一连接（threading.local），避免多连接写冲突。
    写操作通过 _with_db_lock 串行化；锁冲突时由 _connect_db 重试自愈。
    自动检测连接是否已关闭并重建。
    """
    conn = getattr(_db_local, 'conn', None)
    # 检查连接是否有效（未关闭；锁冲突视为失效，交由 _connect_db 重试重建）
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            conn = None  # 连接已关闭（如 conn.close() 遗留）或已锁定，重建
    if conn is None:
        conn = _connect_db()
        _db_local.conn = conn
    return conn


def _with_db_lock(func):
    """装饰器：全局锁串行化所有数据库写操作；锁冲突自动回滚重试。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _db_lock:
            last_err = None
            for attempt in range(1, _BUSY_RETRIES + 1):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if not _is_busy_error(e):
                        raise
                    last_err = e
                    logger.warning(f'SQLite 写操作被锁（第 {attempt}/{_BUSY_RETRIES} 次）: {e}')
                    conn = getattr(_db_local, 'conn', None)
                    if conn is not None:
                        try:
                            conn.rollback()  # 回滚半途事务，避免脏状态带入重试
                        except Exception:
                            pass
                    time.sleep(_BUSY_SLEEP)
            raise last_err
    return wrapper


def _init_db():
    """初始化数据库表结构（幂等；锁冲突自动重试，避免并发初始化中断执行）。"""
    for attempt in range(1, _BUSY_RETRIES + 1):
        try:
            conn = _get_db()
            conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            namespace TEXT NOT NULL DEFAULT 'default',
            title TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_calls TEXT DEFAULT NULL,
            tool_results TEXT DEFAULT NULL,
            source TEXT NOT NULL DEFAULT 'chat',
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_messages_source
            ON messages(session_id, source);
        CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON sessions(user_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            range_start INTEGER NOT NULL,
            range_end INTEGER NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_session
            ON summaries(session_id, created_at DESC);

        -- 用户记忆：跨会话的长期记忆片段（按命名空间隔离，角色卡片互不串扰）
        CREATE TABLE IF NOT EXISTS user_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            namespace TEXT NOT NULL DEFAULT 'default',
            memory_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source_session_id TEXT,
            importance REAL DEFAULT 0.5,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_user_memories
            ON user_memories(user_id, importance DESC);

        -- 上下文打包统计：衡量 token 节省与估算误差（每轮对话一条）
        CREATE TABLE IF NOT EXISTS context_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            namespace TEXT NOT NULL DEFAULT 'default',
            raw_tokens INTEGER DEFAULT 0,
            packed_tokens INTEGER DEFAULT 0,
            actual_prompt_tokens INTEGER DEFAULT 0,
            history_rounds INTEGER DEFAULT 0,
            summaries INTEGER DEFAULT 0,
            memories INTEGER DEFAULT 0,
            recall_items INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_context_stats_ns
            ON context_stats(namespace, created_at);
    """)
            # 迁移：为旧数据库添加 source 字段（重复添加属预期，锁冲突如实抛出交由外层重试）
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'")
            except sqlite3.OperationalError as e:
                if not _is_duplicate_column(e):
                    raise
                logger.debug(f'字段已存在(source)，跳过迁移: {e}')
            # 迁移：为旧数据库添加 namespace 字段（角色卡片独立记忆空间）
            for _tbl in ('sessions', 'user_memories'):
                try:
                    conn.execute(f'ALTER TABLE {_tbl} ADD COLUMN namespace TEXT NOT NULL DEFAULT \'default\'')
                except sqlite3.OperationalError as e:
                    if not _is_duplicate_column(e):
                        raise
                    logger.debug(f'字段已存在(namespace@{_tbl})，跳过迁移: {e}')
            # 迁移：为旧数据库添加 category 字段（用户记忆分类：称呼/音乐/影片/城市/其他）
            try:
                conn.execute("ALTER TABLE user_memories ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
            except sqlite3.OperationalError as e:
                if not _is_duplicate_column(e):
                    raise
                logger.debug(f'字段已存在(category)，跳过迁移: {e}')
            # 迁移：会话管理增强字段（archived 归档 / pinned 置顶，均幂等）
            for _col, _dflt in (("archived", "0"), ("pinned", "0")):
                try:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {_col} INTEGER NOT NULL DEFAULT {_dflt}")
                except sqlite3.OperationalError as e:
                    if not _is_duplicate_column(e):
                        raise
                    logger.debug(f'字段已存在({_col}@sessions)，跳过迁移: {e}')
            # namespace 相关索引（必须在 ALTER 添加列之后再建，否则旧库会报 no such column）
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_sessions_ns
                    ON sessions(user_id, namespace, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_user_memories_ns
                    ON user_memories(user_id, namespace, importance DESC);
                CREATE INDEX IF NOT EXISTS idx_user_memories_cat
                    ON user_memories(user_id, namespace, category, importance DESC);
            """)
            conn.commit()
            return  # 初始化成功
        except sqlite3.OperationalError as e:
            if not _is_busy_error(e):
                logger.error(f'SQLite 数据库初始化失败（非锁冲突）: {e}')
                raise
            logger.warning(f'SQLite 数据库初始化被锁（第 {attempt}/{_BUSY_RETRIES} 次）: {e}')
            time.sleep(_BUSY_SLEEP)
    logger.error(f'SQLite 数据库初始化最终失败（连续 {_BUSY_RETRIES} 次锁冲突）')
    # 不抛出：瞬时锁冲突不影响后续调用（写操作仍由 _with_db_lock 重试自愈）


# 模块加载时自动初始化（失败不阻断导入，避免 codex 执行时数据库被锁导致整体中断）
try:
    _init_db()
except Exception as e:
    logger.error(f'SQLite 初始化异常，继续以懒重试模式运行: {e}')


def _strict_user_scope(uid: str) -> bool:
    """该身份是否按 user_id 严格隔离记忆与会话。

    为什么需要这个判据：单系统模式要求「最近活跃会话被当前身份接管」，多设备/
    刷新后才续得上同一条对话；而公网多用户下这个行为是灾难 —— 普通用户一登录
    就会接管管理员正在聊的会话，等于读到对方全部历史。

    判据：真实注册的普通用户 = 严格隔离；管理员与系统身份（default/unified）
    = 保持旧行为。管理员不严格是刻意的：她自己的机器、自己的历史（含注册账号
    之前的 default 会话）应当照旧可见可续；但即便管理员也不会抢走其他注册
    用户的会话（见 get_or_create_session）。
    """
    try:
        import auth_core

        u = auth_core.get_user(str(uid or ""))
        if not u:
            return False
        return str(u.get("role") or "user") != "admin"
    except Exception:
        return False


def _is_registered_user(uid: str) -> bool:
    """uid 是否为已注册登录账号（区别于浏览器匿名 u_* 与系统 default 桶）。"""
    if not uid or str(uid).strip() in ("", "default"):
        return False
    try:
        import auth_core

        return bool(auth_core.get_user(str(uid)))
    except Exception:
        return False


class ChatMemory:
    """对话记忆管理器。

    每个实例关联一个用户和一个会话，提供消息存储、检索和摘要功能。
    """

    def __init__(self, user_id: str = "default", session_id: str = None,
                 namespace: str = "default"):
        self.user_id = user_id
        self.session_id = session_id
        self.namespace = namespace  # 记忆命名空间：无卡片='default'，角色卡片='role_card:<card_id>'
        self._message_count = 0  # 当前会话消息计数缓存
        self._summary_lock = None  # 懒创建：串行化普通/强制摘要，避免重复覆盖同一区间
        self._llm_client = None  # 可选：用于 LLM 驱动的摘要生成和记忆提取
        self._llm_model = None

    # ==================== 会话管理 ====================

    def set_llm_client(self, client, model_name: str):
        """设置 LLM 客户端，用于生成高质量摘要和提取长期记忆。

        Args:
            client: OpenAI AsyncOpenAI 客户端实例
            model_name: 模型名称
        """
        self._llm_client = client
        self._llm_model = model_name

    async def _supervised_create(self, **kwargs):
        """经 harness 监督运行时执行 LLM 调用（重试/超时/熔断/计量，渠道=memory）。

        harness 不可用时退化为直接调用，原有降级逻辑（关键词摘要/提取）不受影响。
        """
        # 兜底：保证 role=tool 消息带 tool_call_id（与主智能体同一规范函数），
        # 否则发给 OpenAI 兼容提供方会被 400（missing field tool_call_id）
        try:
            from agent import _normalize_tool_rounds
            _msgs = kwargs.get("messages") or []
            if _msgs:
                kwargs = dict(kwargs)
                kwargs["messages"] = _normalize_tool_rounds(_msgs)
        except Exception:
            pass
        try:
            from harness import get_harness
            runtime = get_harness().runtime
        except Exception:
            runtime = None
        if runtime is not None:
            return await runtime.supervise_llm(
                "memory", lambda: self._llm_client.chat.completions.create(**kwargs))
        return await self._llm_client.chat.completions.create(**kwargs)

    async def get_or_create_session(self, title: str = "") -> str:
        """获取或创建当前用户的活动会话。

        Returns:
            session_id
        """
        if self.session_id:
            return self.session_id

        @_with_db_lock
        def _do():
            conn = _get_db()
            now = time.time()

            def _adopt(sid: str):
                """采用某个已有会话为当前会话（调用方已在锁内）。"""
                self.session_id = sid
                conn.execute(
                    "UPDATE sessions SET updated_at=?, is_active=1 WHERE id=?",
                    (now, sid),
                )
                conn.commit()
                # 加载当前消息计数（环境交互消息不计入，与 add_message 保持一致）
                cnt = conn.execute(
                    "SELECT COUNT(*) as cnt FROM messages "
                    "WHERE session_id=? AND source != 'auto'",
                    (sid,),
                ).fetchone()["cnt"]
                self._message_count = cnt
                return sid

            # 1) 续上最近活跃会话：
            #    - 严格身份（普通用户）：只认自己名下的会话，绝不接管别人的；
            #    - 管理员/系统身份：保持单系统旧行为（可接管 default 等旧身份），
            #      但不碰**其他注册用户**的会话——否则管理员一登录就把别人正在聊的
            #      会话抢过来，对方下一句话就开在新会话里了。
            strict = _strict_user_scope(self.user_id)
            rows = conn.execute(
                "SELECT id, user_id FROM sessions "
                "WHERE namespace=? AND is_active=1 AND archived=0 "
                "ORDER BY updated_at DESC LIMIT 20",
                (self.namespace,),
            ).fetchall()
            for r in rows:
                if r["user_id"] == self.user_id:
                    return _adopt(r["id"])
                if strict:
                    continue
                # 非严格身份（管理员/系统）：只接管「系统旧身份桶」（default/空串）的历史——
                # 必须自己是已注册登录身份，且对方是旧桶；其余（别的注册用户、其他浏览器
                # 的匿名 u_* 会话）一律视为他人绝不接管：否则管理员一登录就把访客正在聊的
                # 会话抢走，两人串到同一条对话线上。
                if self.user_id in ("", "default") or not _is_registered_user(self.user_id):
                    continue
                if r["user_id"] not in ("", "default"):
                    continue
                conn.execute(
                    "UPDATE sessions SET user_id=? WHERE id=?",
                    (self.user_id, r["id"]),
                )
                conn.commit()
                return _adopt(r["id"])
            # 2) 兼容旧逻辑：近期活动会话（15 分钟内，仅限当前命名空间）
            recent = now - 900
            row = conn.execute(
                "SELECT id FROM sessions WHERE user_id=? AND namespace=? AND is_active=1 "
                "AND archived=0 AND updated_at>? "
                "ORDER BY updated_at DESC LIMIT 1",
                (self.user_id, self.namespace, recent),
            ).fetchone()
            if row:
                return _adopt(row["id"])

            # 3) 创建新会话
            import uuid
            sid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO sessions (id, user_id, namespace, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, self.user_id, self.namespace, title or f"对话 {datetime.now():%m-%d %H:%M}", now, now),
            )
            conn.commit()
            self.session_id = sid
            self._message_count = 0
            return sid

        return await asyncio.to_thread(_do)

    async def rebind_session_namespace(self, session_id: str, namespace: str) -> None:
        """把某个会话的归属命名空间改掉（换角色卡片时让当前会话跟着人设走）。

        只改标签，不动消息：会话列表/可见性都按 namespace 过滤，不重绑的话
        正在聊的这条会话会在新卡片的历史列表里凭空消失。
        """
        if not session_id or not namespace:
            return

        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute("UPDATE sessions SET namespace=? WHERE id=?",
                         (namespace, session_id))
            conn.commit()

        await asyncio.to_thread(_do)

    async def create_new_session(self, title: str = "") -> str:
        """强制创建全新会话（不复用近期活动会话）。

        用于角色卡片人设变更：旧会话的历史消息会把人设带偏，
        直接开新会话让新系统提示词立即生效。
        """
        @_with_db_lock
        def _do():
            conn = _get_db()
            now = time.time()
            import uuid
            sid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO sessions (id, user_id, namespace, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, self.user_id, self.namespace,
                 title or f"对话 {datetime.now():%m-%d %H:%M}", now, now),
            )
            conn.commit()
            self.session_id = sid
            self._message_count = 0
            return sid
        return await asyncio.to_thread(_do)

    async def set_session_id(self, session_id: str):
        """手动设置会话 ID（用于恢复历史会话）。

        单系统模式：恢复旧设备身份的会话时自动接管（user_id 归一到当前统一用户）。
        """
        self.session_id = session_id
        # 更新会话活动时间并加载当前消息计数
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute(
                "UPDATE sessions SET updated_at=?, is_active=1, user_id=? WHERE id=?",
                (time.time(), self.user_id, session_id),
            )
            cnt = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages "
                "WHERE session_id=? AND source != 'auto'",
                (session_id,),
            ).fetchone()["cnt"]
            conn.commit()
            return cnt
        self._message_count = await asyncio.to_thread(_do)

    async def get_max_message_id(self) -> int:
        """当前会话最新消息 id（0 = 无消息）。对话轮断点恢复的锚点用。"""
        if not self.session_id:
            return 0

        def _do():
            conn = _get_db()
            row = conn.execute(
                "SELECT MAX(id) AS mid FROM messages WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            conn.close()
            return int(row["mid"] or 0) if row else 0
        return await asyncio.to_thread(_do)

    async def delete_messages_after(self, anchor_id: int) -> None:
        """删除当前会话中 id 大于锚点的消息（断点续跑幂等用）。

        仅用于「被热重载打断的对话轮」恢复时，清理该轮可能已半写入的
        assistant/tool 消息；锚点取自本轮开始（用户消息已落库之后），
        因此锚点之后只可能是本中断轮自己的消息，删除后统一重放。
        """
        if not self.session_id or anchor_id < 0:
            return

        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute(
                "DELETE FROM messages WHERE session_id=? AND id>?",
                (self.session_id, anchor_id),
            )
            conn.commit()
            conn.close()
        await asyncio.to_thread(_do)

    async def delete_messages_after_if(self, anchor_id: int, guard=None) -> bool:
        """同 delete_messages_after，但删除前在数据库写锁内调用 guard()。

        guard 返回 False 时放弃删除（返回 False）。用于断点续跑：把「断点仍
        属于本轮」的校验与删除放进同一个临界区，避免并发新对话轮刚写入的用户
        消息被误删。
        """
        if not self.session_id or anchor_id < 0:
            return False

        @_with_db_lock
        def _do():
            if guard is not None and not guard():
                return False
            conn = _get_db()
            conn.execute(
                "DELETE FROM messages WHERE session_id=? AND id>?",
                (self.session_id, anchor_id),
            )
            conn.commit()
            conn.close()
            return True
        return await asyncio.to_thread(_do)

    async def session_belongs_to_namespace(self, session_id: str) -> bool:
        """判断会话是否属于当前命名空间（防止跨角色卡片切换/恢复串记忆）。"""
        if not session_id:
            return False
        def _do():
            conn = _get_db()
            row = conn.execute(
                "SELECT namespace FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            conn.close()
            return bool(row and row["namespace"] == self.namespace)
        return await asyncio.to_thread(_do)

    async def session_visible(self, session_id: str) -> bool:
        """会话对当前身份是否可见：namespace 相符 + （严格身份下）归属自己。

        比 session_belongs_to_namespace 多一层用户归属校验 —— 后者只按角色卡片
        命名空间判定，多用户下普通用户拿到别人的 session_id 就能读全文。
        """
        if not session_id:
            return False
        strict = _strict_user_scope(self.user_id)

        def _do():
            conn = _get_db()
            row = conn.execute(
                "SELECT namespace, user_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            conn.close()
            if not row or row["namespace"] != self.namespace:
                return False
            if row["user_id"] != self.user_id:
                # 别人的会话一律不可见。非严格身份（管理员）额外可见的只是
                # 系统旧身份桶（default/空串）——且自身必须是已注册登录身份；
                # 匿名浏览器 u_* 会话、别的注册用户，一概不可见（防串线）。
                if strict:
                    return False
                if self.user_id in ("", "default") or not _is_registered_user(self.user_id):
                    return False
                if row["user_id"] not in ("", "default"):
                    return False
            return True

        return await asyncio.to_thread(_do)

    async def list_sessions(self, limit: int = 50, query: str = None,
                            include_archived: bool = False) -> list:
        """列出当前命名空间下的所有会话（含消息数/摘要/token 估算/置顶/归档）。

        单系统模式：包含旧的设备级身份（u_*）会话，方便手动切回历史对话；
        会话归属统一用户时会被自动接管。

        Args:
            limit: 返回条数上限
            query: 非空时按标题或消息内容模糊搜索（LIKE，搜索覆盖归档会话）
            include_archived: True 时包含已归档会话（纯列表默认排除）

        Returns:
            [{"id","title","created_at","updated_at","is_active","pinned","archived",
              "message_count","approx_tokens","summary","is_current"}, ...]
            排序：置顶优先，其次按最近活跃。
        """
        def _do():
            conn = _get_db()
            like = f"%{query}%" if query else None
            sql = (
                "SELECT s.id, s.title, s.created_at, s.updated_at, s.is_active, "
                "s.pinned, s.archived, "
                "COALESCE(m.cnt, 0) AS message_count, "
                "COALESCE(m.chars, 0) AS content_chars, "
                "(SELECT st.summary_text FROM summaries st "
                " WHERE st.session_id = s.id "
                " ORDER BY st.range_end DESC, st.id DESC LIMIT 1) AS summary "
                "FROM sessions s "
                "LEFT JOIN (SELECT session_id, COUNT(*) AS cnt, "
                "SUM(LENGTH(content)) AS chars FROM messages "
                "WHERE source != 'auto' GROUP BY session_id) m ON m.session_id = s.id "
                "WHERE s.namespace=? "
            )
            params: list = [self.namespace]
            if _strict_user_scope(self.user_id) or not _is_registered_user(self.user_id):
                # 普通用户 & 匿名浏览器身份：会话列表只含自己的 —— 混入别人的
                # 会话（管理员/旧设备身份/别的浏览器）即隐私泄漏 + 一键串线。
                sql += "AND s.user_id=? "
                params.append(self.user_id)
            else:
                # 管理员/系统身份：只列出自己的 + 系统旧身份桶（default/空串）历史，
                # 其余一律排除 —— 实测漏洞：管理员列表混进普通用户会话，点开即接管。
                sql += "AND (s.user_id=? OR s.user_id IN ('', 'default')) "
                params.append(self.user_id)
            # 搜索时覆盖归档会话（用户搜历史内容通常也包括归档）；纯列表才默认排除
            if not include_archived and not like:
                sql += "AND s.archived=0 "
            if like:
                sql += ("AND (s.title LIKE ? OR EXISTS (SELECT 1 FROM messages mm "
                        "WHERE mm.session_id = s.id AND mm.source != 'auto' "
                        "AND mm.content LIKE ?)) ")
                params += [like, like]
            sql += "ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            sessions = []
            for r in rows:
                s = dict(r)
                s["created_at"] = datetime.fromtimestamp(s["created_at"]).isoformat()
                s["updated_at"] = datetime.fromtimestamp(s["updated_at"]).isoformat()
                # token 估算：中英混合消息约 2 字符/token（沿用 estimate_tokens 的量级）
                s["approx_tokens"] = int((s.pop("content_chars") or 0) / 2)
                _sm = (s.get("summary") or "").strip()
                s["summary"] = _sm[:120] + ("…" if len(_sm) > 120 else "")
                s["is_current"] = (s["id"] == self.session_id)
                sessions.append(s)
            conn.close()
            return sessions
        return await asyncio.to_thread(_do)

    async def get_session_history_pairs(self, session_id: str,
                                        max_rounds: int = None) -> list:
        """获取指定会话的完整 user/ai 轮次历史（回滚继承/前端渲染用）。

        同一用户发言后的多条 assistant 消息合并为一条 ai 文本；工具循环的
        中间轮（带工具调用）与空消息会被跳过，只保留每轮最终回复，
        避免恢复出的历史是"我先看看…"之类的过程话而非结论。
        AI 主动发言（无 user）保留为空白 user 轮次。

        Returns:
            [{"user": "...", "ai": "..."}, ...]
        """
        if not session_id:
            return []

        def _do():
            conn = _get_db()
            rows = conn.execute(
                "SELECT role, content, tool_calls FROM messages "
                "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
                "AND role IN ('user','assistant') ORDER BY id",
                (session_id,),
            ).fetchall()
            conn.close()
            history = _iter_turn_texts([dict(r) for r in rows])
            if max_rounds:
                history = history[-max_rounds:]
            return history
        return await asyncio.to_thread(_do)

    async def get_session_summary(self, session_id: str = None) -> str:
        """获取指定会话（默认当前会话）的最新摘要文本。"""
        sid = session_id or self.session_id
        if not sid:
            return ""

        def _do():
            conn = _get_db()
            row = conn.execute(
                "SELECT summary_text FROM summaries WHERE session_id=? "
                "ORDER BY range_end DESC, id DESC LIMIT 1",
                (sid,),
            ).fetchone()
            conn.close()
            return row["summary_text"] if row else ""
        return await asyncio.to_thread(_do)

    async def update_title(self, title: str, session_id: str = None):
        """更新会话标题（默认当前会话；可指定任意会话 ID 用于历史会话重命名）。"""
        sid = session_id or self.session_id
        if not sid or not title:
            return
        def _do():
            conn = _get_db()
            conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_do)

    @staticmethod
    def _is_default_title(title: str) -> bool:
        """判断是否为默认自动标题（'对话 MM-DD HH:MM'，即未手动重命名/未自动生成）。"""
        return bool(title) and title.startswith("对话 ") and len(title) <= 20

    async def set_session_pinned(self, session_id: str, pinned: bool = True):
        """置顶 / 取消置顶指定会话（置顶会话在列表中优先展示）。"""
        if not session_id:
            return
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute("UPDATE sessions SET pinned=? WHERE id=?", (1 if pinned else 0, session_id))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_do)

    async def set_session_archived(self, session_id: str, archived: bool = True):
        """归档 / 取消归档指定会话。

        归档会话不出现在默认会话列表、不会被 get_or_create_session 自动复用；
        消息与摘要仍在库中，取消归档后可完整恢复。
        """
        if not session_id:
            return
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute("UPDATE sessions SET archived=? WHERE id=?", (1 if archived else 0, session_id))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_do)

    # ==================== 消息存储 ====================

    async def add_message(self, role: str, content: str,
                          tool_calls: list = None, tool_results: list = None,
                          source: str = "chat"):
        """添加一条消息到当前会话。

        Args:
            role: user / assistant / system / tool
            content: 消息内容
            tool_calls: tool_call 信息列表（assistant 消息可能携带）
            tool_results: 工具调用结果列表（tool 消息）
            source: 消息来源：'chat'=用户直接输入、
                'auto'=环境交互（感知触发/自主行为等）。
                环境交互消息只做持久化存储，不计入消息计数、不进 LLM 上下文、
                不参与摘要——短期记忆只与用户直接输入有关，环境交互由记忆系统兜底存储。
        """
        if not self.session_id:
            await self.get_or_create_session()

        @_with_db_lock
        def _do():
            conn = _get_db()
            now = time.time()
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_results, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.session_id, role, content,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    json.dumps(tool_results, ensure_ascii=False) if tool_results else None,
                    source,
                    now,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, self.session_id),
            )
            # 标题自动生成：首条用户消息到达且标题仍为默认格式时，
            # 用消息前 24 字生成语义标题（锁内完成，避免额外并发）
            if role == "user" and source == "chat":
                _row = conn.execute(
                    "SELECT title FROM sessions WHERE id=?", (self.session_id,),
                ).fetchone()
                if _row and self._is_default_title(_row["title"]):
                    _text = (content or "").replace("\n", " ").strip()
                    if _text:
                        _title = _text[:24] + ("…" if len(_text) > 24 else "")
                        conn.execute(
                            "UPDATE sessions SET title=? WHERE id=?", (_title, self.session_id),
                        )
            conn.commit()
            # 环境交互消息不计入摘要计数（避免高频快照触发无意义的摘要生成）
            if source != "auto":
                self._message_count += 1

        await asyncio.to_thread(_do)

        # 检查是否需要生成摘要
        if self._message_count > 0 and self._message_count % SUMMARY_INTERVAL == 0:
            await self.maybe_summarize()

    async def maybe_summarize(self):
        """检查并在需要时生成对话摘要（后台执行，绝不阻塞对话轮）。

        摘要只影响「下一次」上下文构建：把它放到后台任务里，用户发消息
        后立即进入 LLM 对话，不再被 8~50s 的摘要 LLM 调用卡住首句。
        """
        # 使用缓存的 _message_count，避免 COUNT(*) 查询
        if self._message_count < SUMMARY_THRESHOLD:
            return
        await self._schedule_background_summary(force=False)

    async def force_summarize(self, include_auto: bool = False,
                              auto_prefix: tuple = ()) -> bool:
        """强制生成一次增量摘要（跳过固定间隔），返回是否已调度后台执行。

        用途：一轮「带工具执行的任务」结束、或子智能体/媒体完成汇报后，
        立即把"已完成"状态固化进摘要——否则后续主动对话会读到过期的
        "进行中"摘要，与刚完成的执行结果自相矛盾。

        2026-09-01 改为后台执行：摘要生成是 LLM 调用（实测平均约 8s、
        峰值 50s+），此前在上下文构建 / 消息入库时同步等待，是用户
        发消息后迟迟不出首句的主因。摘要只影响下一轮上下文，后台完成
        即可；当前轮真正新鲜的工具执行结果由 get_recent_work_digest
        直接注入，不受影响。

        Args:
            include_auto: 是否把 auto 消息一并纳入摘要。默认只汇总用户直接
                输入，避免高频环境快照噪音污染摘要。
            auto_prefix: include_auto=True 时，仅纳入 content 以此前缀开头的
                auto 消息（如「【子智能体汇报」）；其余 auto 噪音仍被排除。
        """
        return await self._schedule_background_summary(
            force=True, include_auto=include_auto, auto_prefix=auto_prefix)

    def _get_summary_lock(self) -> asyncio.Lock:
        """懒创建摘要生成锁（兼容无事件循环时构造实例的场景）。"""
        if self._summary_lock is None:
            self._summary_lock = asyncio.Lock()
        return self._summary_lock

    async def _schedule_background_summary(self, *, force: bool,
                                           include_auto: bool = False,
                                           auto_prefix: tuple = ()) -> bool:
        """把摘要生成调度为后台任务后立即返回，绝不阻塞对话关键路径。

        摘要属于"下一次上下文构建才消费"的陈旧状态，晚几秒完成无影响；
        串行化仍由 _summary_lock 保证（重叠触发时后一个会等前一个完成，
        并按最新摘要范围增量补做，不会重复覆盖同一区间）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("无事件循环，摘要生成跳过（下个触发点会补做）")
            return False

        async def _job():
            async with self._get_summary_lock():
                if force:
                    await self._force_summary_body(include_auto, auto_prefix)
                else:
                    await self._maybe_summary_body()

        task = loop.create_task(_job())

        def _on_done(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
                if exc is not None:
                    logger.warning(f"后台摘要任务异常: {exc}")
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_on_done)
        return True

    async def _maybe_summary_body(self):
        """maybe_summarize 的实际主体（调用方须持有 _summary_lock）。"""
        # 检查最新的摘要覆盖范围
        def _get_last_summary_range():
            conn = _get_db()
            row = conn.execute(
                "SELECT range_end FROM summaries WHERE session_id=? ORDER BY range_end DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            conn.close()
            return row["range_end"] if row else 0

        last_end = await asyncio.to_thread(_get_last_summary_range)

        def _get_messages_since(start_id):
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, role, content FROM messages "
                "WHERE session_id=? AND id > ? "
                "AND (source != 'auto' OR role = 'assistant') ORDER BY id",
                (self.session_id, start_id),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]

        msgs = await asyncio.to_thread(_get_messages_since, last_end)
        if len(msgs) >= SUMMARY_INTERVAL * 2:
            await self._generate_summary(msgs)

    async def _force_summary_body(self, include_auto: bool, auto_prefix: tuple):
        """force_summarize 的实际主体（调用方须持有 _summary_lock）。"""
        if not self.session_id:
            return

        def _last_range():
            conn = _get_db()
            row = conn.execute(
                "SELECT MAX(range_end) AS m FROM summaries WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            conn.close()
            return row["m"] if row and row["m"] is not None else 0

        last_end = await asyncio.to_thread(_last_range)

        def _get_msgs():
            conn = _get_db()
            if include_auto:
                conds = ["session_id=?", "id > ?"]
                params = [self.session_id, last_end]
                if auto_prefix:
                    pref = []
                    for p in auto_prefix:
                        pref.append("content LIKE ?")
                        params.append(str(p) + "%")
                    conds.append("(source != 'auto' OR " + " OR ".join(pref) + ")")
                else:
                    conds.append("source != 'auto'")
                rows = conn.execute(
                    "SELECT id, role, content FROM messages WHERE " +
                    " AND ".join(conds) + " ORDER BY id",
                    params,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE session_id=? AND id > ? "
                    "AND source != 'auto' ORDER BY id",
                    (self.session_id, last_end),
                ).fetchall()
            conn.close()
            return [dict(r) for r in rows]

        msgs = await asyncio.to_thread(_get_msgs)
        if not msgs:
            return
        await self._generate_summary(msgs[-SUMMARY_INTERVAL * 2:])

    async def _has_unsaved_work_round(self) -> bool:
        """未摘要区间内是否出现过「带工具执行或完成汇报」的内容。

        用于上下文构建前发现"任务刚做完但摘要还停留在'进行中'"的情况，
        触发一次强制摘要，保证后续对话（含系统自动触发的主动对话）
        看到的摘要是最新的。

        判定放宽：只要上次摘要之后有工具执行（tool 消息或带工具调用的
        assistant 消息）或子智能体完成汇报，就视为存在未固化的"已完成"
        状态，需要补摘要。不再要求区间最后一条必须是 assistant——
        实际上一轮工具对话的末条往往是 tool 结果，旧判定会漏掉该场景，
        导致下一轮继续读到过期的"进行中/被阻止"摘要而重复做事。
        """
        if not self.session_id:
            return False

        def _check():
            conn = _get_db()
            row = conn.execute(
                "SELECT MAX(range_end) AS m FROM summaries WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            last_end = row["m"] if row and row["m"] is not None else 0
            rows = conn.execute(
                "SELECT role, tool_calls, content FROM messages "
                "WHERE session_id=? AND id > ? "
                "AND (source != 'auto' OR content LIKE '【子智能体汇报%') "
                "ORDER BY id",
                (self.session_id, last_end),
            ).fetchall()
            conn.close()
            rows = [dict(r) for r in rows]
            if not rows:
                return False
            has_tool_work = any(r["role"] == "tool" for r in rows) or any(
                _row_has_tool_calls(r) for r in rows)
            has_report = any(
                (r["content"] or "").startswith("【子智能体汇报") for r in rows)
            return has_tool_work or has_report

        return await asyncio.to_thread(_check)

    async def _generate_summary(self, messages: list):
        """生成对话摘要并存储。优先使用 LLM，降级为关键词提取。

        每次生成摘要时，会传入前一次摘要作为参考，让 LLM 做增量式补充，
        避免重复描述已经总结过的内容。
        """
        if not messages:
            return

        # 构建简要对话文本
        dialog_text = ""
        for m in messages[-SUMMARY_INTERVAL * 2:]:
            content = (m["content"] or "").strip()
            if not content:
                continue  # 静默工具轮等空消息不参与摘要
            # 工具结果单独标记（LLM 摘要可理解；关键词降级摘要据此过滤噪音）
            role_tag = ("工具" if m["role"] == "tool"
                        else ("用户" if m["role"] == "user" else "AI"))
            # 过滤纯游戏事件消息，避免摘要混入大量系统通知
            if m["role"] == "system" and content.startswith("[游戏]"):
                dialog_text += f"事件: {content}\n"
            else:
                dialog_text += f"{role_tag}: {content[:200]}\n"

        # 获取前一次摘要，传递给 LLM 做增量式补充
        prev_summary = ""
        if self._llm_client and self._llm_model:
            prev_summary = await self._get_latest_summary_text()

        # 优先使用 LLM 生成语义摘要
        if self._llm_client and self._llm_model:
            summary = await self._llm_summarize(dialog_text, prev_summary)
        else:
            summary = self._keyword_summarize(dialog_text)

        # 截断过长摘要（累计式摘要上限：自包含覆盖全会话，单条可容 600 字）
        if len(summary) > 600:
            summary = summary[:597] + "..."

        @_with_db_lock
        def _save():
            conn = _get_db()
            conn.execute(
                "INSERT INTO summaries (session_id, summary_text, range_start, range_end, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self.session_id,
                    summary,
                    messages[0]["id"],
                    messages[-1]["id"],
                    time.time(),
                ),
            )
            conn.commit()
            logger.info(f"会话 {self.session_id} 生成摘要: {summary[:100]}...")

        await asyncio.to_thread(_save)

    async def _get_latest_summary_text(self) -> str:
        """获取当前会话最新的一条摘要文本。"""
        def _do():
            conn = _get_db()
            row = conn.execute(
                "SELECT summary_text FROM summaries WHERE session_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            conn.close()
            return row["summary_text"] if row else ""
        return await asyncio.to_thread(_do)

    async def _llm_summarize(self, dialog_text: str, prev_summary: str = "") -> str:
        """使用 LLM 生成语义摘要。

        Args:
            dialog_text: 本次需要总结的对话文本
            prev_summary: 前一次摘要，用于合并成自包含的最新摘要。

        2026-08-29 改为「累计式」：输出 = 旧摘要 + 新内容的合并版，而不是只写增量。
        这样最新一条摘要本身就覆盖全会话，不会因增量链断裂/“同上”而丢失早期信息
        （此前工程任务中模型只看到 120 字增量，隔几轮就忘了任务初衷）。
        """
        system_msg = (
            "你是对话摘要助手。请把「旧摘要」与「新对话内容」合并成一份"
            "**自包含的最新摘要**（4-8句中文）：保留旧摘要中仍然重要的信息，"
            "并纳入新对话新增的关键内容——话题变迁、用户透露的新信息、"
            "重要决策/事件、任务目标与当前进度。忽略日常寒暄和已过时的细节。"
        )
        if prev_summary:
            user_msg = (
                f'【旧摘要】\n{prev_summary}\n\n'
                f'【新对话内容】\n{dialog_text}\n\n'
                f'请输出合并后的完整最新摘要：涵盖旧摘要中的重要信息与新对话的新增内容。'
                f'如果新对话没有新信息，直接原样复用旧摘要。只输出摘要正文，不要任何前缀。'
            )
        else:
            user_msg = f'请总结以下对话（4-8句中文，覆盖目标、进展与关键信息）：\n{dialog_text}'

        try:
            response = await self._supervised_create(
                model=self._llm_model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=150,
            )
            content = response.choices[0].message.content.strip()
            if not content or content == "同上":
                return prev_summary  # 无新内容，复用前次摘要
            return content
        except Exception as e:
            logger.warning(f"LLM 摘要生成失败，降级为关键词提取: {e}")
            return self._keyword_summarize(dialog_text)

    @staticmethod
    def _keyword_summarize(dialog_text: str) -> str:
        """降级方案：从对话中提取关键话题和事件作为摘要。

        优先提取用户消息中的实质内容，过滤寒暄和语气词。
        """
        lines = dialog_text.strip().split("\n")
        topics = []
        filler_words = {"嗯", "啊", "哦", "好", "好的", "行", "可以", "对", "是的", "没错",
                        "哈哈", "嘿嘿", "嘻嘻", "呵呵", "谢谢", "不客气", "知道了", "明白了"}

        for line in lines:
            # 提取角色和内容
            if ": " not in line:
                continue
            role, content = line.split(": ", 1)
            # 跳过工具结果行（$ 命令、✅/✘ 输出、日志片段等原始噪音——
            # 关键词降级摘要没有语义能力，硬拼只会污染上下文）
            if role.strip() == "工具":
                continue
            content = content.strip()
            # 跳过太短或纯寒暄的
            if len(content) < 6 or content in filler_words:
                continue
            # 跳过游戏事件（[游戏] 前缀）
            if content.startswith("[游戏]"):
                continue
            # 截取有意义的前半部分
            if len(content) > 80:
                content = content[:77] + "..."
            topics.append(content)

        if not topics:
            return "(暂无重要话题)"
        # 最多保留6条，用分号连接
        return "；".join(topics[:6])

    # ==================== 记忆检索 ====================

    async def get_context_messages(self, recent_n: int = None) -> list:
        """获取最近的消息列表，结合摘要提供上下文。

        Returns:
            OpenAI 消息格式的列表: [{"role": ..., "content": ...}, ...]
        """
        if not self.session_id:
            return []

        # DB 主源路径需要更多消息才能覆盖「最近几轮 + 被打断轮的工具轮」：
        # 单轮工具循环可占 60+ 条消息（实测最新一轮 42 条），若只回读 60 条，
        # 更早的用户轮会被整批挤出窗口，「保底 N 轮」就没有数据可保。
        # 这里读 400 条、取最近 300 条，再由 short_term_min_rounds 保证最近轮次
        # 必定入选；预算仍由 short_term_max_tokens / 单轮字符上限兜底。
        recent_n = recent_n or max(RECENT_MESSAGE_COUNT, 300)

        def _do():
            conn = _get_db()
            messages = []

            # 仅在消息数超过阈值时才加载摘要（小于阈值的会话不可能有摘要）
            if self._message_count >= SUMMARY_THRESHOLD:
                summary_rows = conn.execute(
                    "SELECT summary_text FROM summaries WHERE session_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (self.session_id, MAX_SUMMARIES),
                ).fetchall()
                if summary_rows:
                    summary_texts = [s["summary_text"] for s in summary_rows]
                    # 合并多条摘要，限制总长度避免浪费 token
                    combined = "；".join(summary_texts)
                    if len(combined) > 600:
                        combined = combined[:597] + "..."
                    summary_text = "以下是之前的对话摘要：\n" + combined
                    messages.append({
                        "role": "system",
                        "content": summary_text,
                    })

            # 获取最近消息（排除环境交互消息：短期记忆只与用户直接输入有关，
            # 环境交互内容由记忆系统兜底存储，不进 LLM 上下文避免重复内容僵化。
            # 2026-08-30 例外：AI 自己的结论若被标记为 auto（感知/UI 触发轮），
            # 也必须保留——否则"上一轮自己说的结论"会被当成噪音丢掉。
            msg_rows = conn.execute(
                "SELECT role, content, tool_calls, tool_results FROM messages "
                "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
                "ORDER BY id DESC LIMIT ?",
                (self.session_id, recent_n),
            ).fetchall()[::-1]  # 反转为正序

            conn.close()

            for row in msg_rows:
                msg = {"role": row["role"], "content": row["content"]}
                if row["tool_calls"]:
                    try:
                        msg["tool_calls"] = json.loads(row["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if row["tool_results"]:
                    try:
                        msg["tool_results"] = json.loads(row["tool_results"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                messages.append(msg)

            return messages

        return await asyncio.to_thread(_do)

    async def get_history_for_llm(self, max_messages: int = 10) -> list:
        """获取用于 LLM 上下文的历史消息（简化格式）。

        只保留每轮最终回复（跳过工具循环的中间轮与空消息），并保证
        取到足够多的原始消息再配对，避免长工具轮把历史挤没。

        Returns:
            [{"user": "...", "ai": "..."}, ...]  兼容现有的 history 格式
        """
        if not self.session_id:
            return []

        def _do():
            conn = _get_db()
            rows = conn.execute(
                "SELECT role, content, tool_calls FROM messages "
                "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
                "AND role IN ('user','assistant') "
                "ORDER BY id DESC LIMIT ?",
                (self.session_id, max(max_messages * 10, 200)),
            ).fetchall()[::-1]
            conn.close()
            history = _iter_turn_texts([dict(r) for r in rows])
            return history[-max_messages:]  # 只保留最近 N 轮

        return await asyncio.to_thread(_do)

    # ==================== 用户长期记忆 ====================

    @staticmethod
    def infer_category(text: str) -> str:
        """根据文本内容推断用户记忆类别（称呼/音乐/影片/城市/其他）。

        优先检查显式标记（LLM 提取时可能带 [称呼] 之类前缀），其次关键词匹配。
        """
        if not text:
            return "其他"
        text_l = text.lower()
        for cat in PREFERENCE_CATEGORIES:
            if ("[" + cat + "]") in text or ("[" + cat + "]") in text_l:
                return cat
        for cat, kws in _CATEGORY_KEYWORDS.items():
            for kw in kws:
                if kw in text_l:
                    return cat
        return "其他"

    @staticmethod
    def strip_category_marker(text: str) -> str:
        """去掉记忆中可能残留的类别标记前缀，只保留内容本身。"""
        import re as _re
        m = _re.match(r"^\s*(?:\[|【)\s*(称呼|音乐|影片|城市|其他)\s*(?:\]|】)\s*(.*)$", text, _re.S)
        if m and m.group(2).strip():
            return m.group(2).strip()
        return text

    async def save_user_memory(self, memory_text: str, importance: float = 0.5,
                               category: str = None):
        """保存用户相关的长期记忆片段。

        若已存在相同内容的记忆，则只更新重要性和访问时间，避免重复入库。

        入库前统一过滤噪音：空内容、模型思维链泄漏（<think>/</think> 残留）、
        "无/同上"等无信息量回复一律丢弃，防止垃圾记忆污染长期记忆层。

        Args:
            memory_text: 记忆内容
            importance: 重要性评分 (0.0-1.0)
            category: 记忆类别（称呼/音乐/影片/城市/其他）；None 时自动推断。
        """
        memory_text = self.strip_category_marker(memory_text or "").strip()
        memory_text = _clean_think_markers(memory_text)
        if _is_memory_noise(memory_text):
            return
        category = category or self.infer_category(memory_text)

        @_with_db_lock
        def _do():
            conn = _get_db()
            now = time.time()
            existing = conn.execute(
                "SELECT id FROM user_memories WHERE user_id=? AND namespace=? AND memory_text=?",
                (self.user_id, self.namespace, memory_text),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE user_memories SET importance=?, last_accessed=?, source_session_id=?, category=? "
                    "WHERE id=?",
                    (max(importance, 0.5), now, self.session_id, category, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO user_memories (user_id, namespace, memory_text, category, source_session_id, "
                    "importance, created_at, last_accessed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.user_id, self.namespace, memory_text, category, self.session_id,
                     importance, now, now),
                )
            conn.commit()

        await asyncio.to_thread(_do)

    async def get_user_memories(self, limit: int = 5, category: str = None) -> list:
        """获取当前命名空间下用户最重要的长期记忆片段。

        Args:
            limit: 返回条数上限
            category: 按类别过滤（称呼/音乐/影片/城市/其他）；None=返回全部
        """
        @_with_db_lock
        def _do():
            conn = _get_db()
            now = time.time()
            sql = (
                # 稳定排序：不用 last_accessed（读取即刷新，顺序每轮重洗会让
                # LLM 前缀缓存每轮全失效）；importance 优先、id 作确定性次序
                "SELECT id, memory_text, importance, category FROM user_memories "
                "WHERE user_id=? AND namespace=?"
                + (" AND category=?" if category else "")
                + " ORDER BY importance DESC, id DESC LIMIT ?"
            )
            params = [self.user_id, self.namespace]
            if category:
                params.append(category)
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            # 更新访问时间
            if rows:
                ids = [r["id"] for r in rows]
                conn.execute(
                    "UPDATE user_memories SET last_accessed=? WHERE id IN ({})".format(
                        ",".join("?" * len(ids))
                    ),
                    [now] + ids,
                )
            conn.commit()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(_do)

    async def extract_and_save_memories(self, user_message: str, ai_response: str):
        """从对话中提取用户偏好/信息并保存为长期记忆。

        优先使用 LLM 智能提取，降级为简单关键词匹配。
        """
        if self._llm_client and self._llm_model:
            await self._llm_extract_memories(user_message, ai_response)
        else:
            self._keyword_extract_memories(user_message, ai_response)

    async def _llm_extract_memories(self, user_message: str, ai_response: str):
        """使用 LLM 智能提取用户记忆点。

        注意：必须只提取「用户本人明确陈述或确认」的真实信息。
        AI 在角色扮演/对话中对用户的描述、推测、虚构设定一律不得入库。
        """
        prompt = (
            f'你是记忆提取助手。从对话中提取**用户本人明确说出或明确确认**的关于自己的真实信息'
            f'（偏好、身份、职业、习惯、经历、真实生活细节）。\n\n'
            f'【严格规则】\n'
            f'1. 只能提取用户自己明确陈述、或明确承认的信息。\n'
            f'2. AI 对用户的描述、外貌/能力设定、推测或想象（例如"你的指尖泛起微光"）一律忽略，'
            f'除非用户亲口承认那是事实。\n'
            f'3. AI 的愿望、感受、行动不是用户的信息（例如"我想和你一起探索"是 AI 的想法，不提取）。\n'
            f'4. 虚构场景、游戏剧情、比喻修辞、AI 编造的环境描写一律不提取。\n'
            f'5. 若用户没有透露任何个人信息，只回复"无"。\n\n'
            f'【对话】\n'
            f'用户: {user_message[:500]}\n'
            f'AI: {ai_response[:300]}\n\n'
            f'请只输出用户明确透露的信息，每行一条、简洁短句，不要编号或加前缀；'
            f'拿不准的信息不要输出。如果没有，只回复"无"。'
            f'如某条信息可归类为以下任一类，请在行首用方括号标注类别：'
            f'[称呼]、[音乐]、[影片]、[城市]；无法归类的不用标注。'
        )
        try:
            response = await self._supervised_create(
                model=self._llm_model,
                messages=[
                    {"role": "system", "content": "你是严格的记忆提取助手。只提取用户本人明确陈述或确认的真实信息，绝不采信 AI 对用户的描述、推测或虚构设定。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=150,
            )
            content = response.choices[0].message.content.strip()
            content = _clean_think_markers(content)
            if content and content not in _MEMORY_NOISE:
                for line in content.split("\n"):
                    line = line.strip()
                    line = _clean_think_markers(line)
                    if line and len(line) > 2 and not _is_memory_noise(line):
                        await self.save_user_memory(line, importance=0.7)
                        logger.info(f"LLM 提取用户记忆({ChatMemory.infer_category(line)}): {line}")
        except Exception as e:
            logger.warning(f"LLM 记忆提取失败，降级为关键词提取: {e}")
            self._keyword_extract_memories(user_message, ai_response)

    def _keyword_extract_memories(self, user_message: str, ai_response: str):
        """降级方案：关键词匹配提取用户信息。"""
        patterns = {
            "我叫": "用户自称",
            "我是": "用户身份",
            "我喜欢": "用户偏好-喜欢",
            "我不喜欢": "用户偏好-不喜欢",
            "我的": "用户信息",
            "我住": "用户住址",
            "我工作": "用户工作",
            "我从事": "用户职业",
        }

        async def _save():
            for keyword, category in patterns.items():
                if keyword in user_message:
                    idx = user_message.find(keyword)
                    snippet = user_message[idx:idx + 80].strip()
                    memory = f"[{category}] {snippet}"
                    await self.save_user_memory(memory, importance=0.7)
                    logger.info(f"提取用户记忆: {memory}")

        # 在后台线程执行
        import asyncio as _asyncio
        try:
            _asyncio.create_task(_save())
        except Exception:
            pass


    # ==================== 主动回忆（按需相关性检索） ====================

    @staticmethod
    def _tokenize(text: str) -> set:
        """轻量中文分词：按 2-gram 取连续 CJK 片段 + 英文单词小写。

        不引入 jieba 等外部依赖（降低部署成本），2-gram 对中文短句关键词匹配足够。
        """
        import re as _re
        tokens = set()
        # 英文/数字单词
        for w in _re.findall(r"[A-Za-z0-9]+", text or ""):
            w = w.lower()
            if len(w) >= 2:
                tokens.add(w)
        # 中文 2-gram（连续汉字段）
        for seg in _re.findall(r"[\u4e00-\u9fff]+", text or ""):
            if len(seg) == 1:
                tokens.add(seg)
            for i in range(len(seg) - 1):
                tokens.add(seg[i:i + 2])
        return tokens

    def _recall_settings(self) -> dict:
        """读取主动回忆配置（settings.json 的 memory 段），失败用默认值。"""
        try:
            with open(BASE_DIR / "settings.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            mem = (cfg.get("memory") or {}) or {}
            return {
                "recall_enabled": bool(mem.get("recall_enabled", RECALL_ENABLED)),
                "recall_top_k": int(mem.get("recall_top_k", RECALL_TOP_K) or RECALL_TOP_K),
                "recall_max_chars": int(mem.get("recall_max_chars", RECALL_MAX_CHARS) or RECALL_MAX_CHARS),
            }
        except Exception:
            return {"recall_enabled": RECALL_ENABLED,
                    "recall_top_k": RECALL_TOP_K,
                    "recall_max_chars": RECALL_MAX_CHARS}

    def _hierarchical_settings(self) -> dict:
        """读取分层记忆打包配置（settings.json -> memory 段覆盖，失败用默认值）。"""
        try:
            with open(BASE_DIR / "settings.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            mem = (cfg.get("memory") or {}) or {}
        except Exception:
            mem = {}
        defaults = {
            'hierarchical_packing': HIERARCHICAL_PACKING,
            'short_term_max_tokens': SHORT_TERM_MAX_TOKENS,
            'short_term_max_chars_per_round': SHORT_TERM_MAX_CHARS_PER_ROUND,
            'short_term_min_rounds': SHORT_TERM_MIN_ROUNDS,
            'short_term_max_chars_per_tool': SHORT_TERM_MAX_CHARS_PER_TOOL,
            'short_term_max_chars_per_tool_call': SHORT_TERM_MAX_CHARS_PER_TOOL_CALL,
            'short_term_keep_last_tools': SHORT_TERM_KEEP_LAST_TOOLS,
            'summary_max_tokens': SUMMARY_MAX_TOKENS,
            'summary_max_chars_per_item': SUMMARY_MAX_CHARS_PER_ITEM,
            'long_term_max_tokens': LONG_TERM_MAX_TOKENS,
            'long_term_top_k': LONG_TERM_TOP_K,
            'recall_max_tokens': RECALL_MAX_TOKENS,
            'record_stats': RECORD_CONTEXT_STATS,
        }
        out = {}
        for k, default in defaults.items():
            v = mem.get(k, default)
            out[k] = bool(v) if k in ('hierarchical_packing', 'record_stats') else int(v)
        return out

    async def recall_memories(self, query: str, limit: int = None,
                              include_messages: bool = True,
                              include_summaries: bool = True) -> list:
        """按当前用户消息检索相关记忆（主动回忆）。

        在长期记忆表 + 历史摘要 + 近期对话消息中做关键词相关性检索，
        返回按相关度排序的记忆片段。检索不到相关内容时返回空列表。

        Args:
            query: 用户当前消息（用于提取关键词）
            limit: 返回条数上限（默认取配置 recall_top_k）
            include_messages: 是否检索近期对话消息（主动回忆最近说过的话）
            include_summaries: 是否检索历史摘要

        Returns:
            [{"kind": "memory"|"summary"|"message", "text": str, "score": float, "category": str}, ...]
        """
        cfg = self._recall_settings()
        if not cfg["recall_enabled"]:
            return []
        limit = limit or cfg["recall_top_k"]
        q = (query or "").strip()
        if not q or len(q) < 2:
            return []
        q_tokens = self._tokenize(q)
        if not q_tokens:
            return []

        def _do():
            conn = _get_db()
            results = []

            def _score(text: str) -> float:
                if not text:
                    return 0.0
                t = self._tokenize(text)
                if not t:
                    return 0.0
                inter = len(t & q_tokens)
                if inter == 0:
                    return 0.0
                # 相关度 = 命中 token 数 / 查询 token 数（覆盖率），弱惩罚长文本
                cov = inter / (len(q_tokens) + 2)
                return cov

            # 1) 长期用户记忆（importance 加权）
            try:
                rows = conn.execute(
                    "SELECT memory_text, importance, category FROM user_memories "
                    "WHERE user_id=? AND namespace=? ORDER BY importance DESC, id DESC LIMIT 100",
                    (self.user_id, self.namespace),
                ).fetchall()
                for r in rows:
                    base = _score(r["memory_text"])
                    if base > 0:
                        score = base * (0.6 + 0.4 * float(r["importance"] or 0.5))
                        results.append({"kind": "memory", "text": r["memory_text"],
                                        "score": score,
                                        "category": r["category"] or "其他"})
            except Exception:
                pass

            # 2) 历史摘要（跨会话，最多取最近 50 条）
            if include_summaries:
                try:
                    srows = conn.execute(
                        "SELECT s.summary_text, s.created_at FROM summaries s "
                        "JOIN sessions se ON s.session_id = se.id "
                        "WHERE se.user_id=? AND se.namespace=? "
                        "ORDER BY s.created_at DESC LIMIT 50",
                        (self.user_id, self.namespace),
                    ).fetchall()
                    for r in srows:
                        base = _score(r["summary_text"])
                        if base > 0:
                            results.append({"kind": "summary", "text": r["summary_text"],
                                            "score": base * 0.85, "category": "其他"})
                except Exception:
                    pass

            # 3) 近期对话消息（主动回忆最近说过的话；排除环境交互 auto）
            if include_messages:
                try:
                    mrows = conn.execute(
                        "SELECT m.role, m.content FROM messages m "
                        "JOIN sessions se ON m.session_id = se.id "
                        "WHERE se.user_id=? AND se.namespace=? AND m.source != 'auto' "
                        "ORDER BY m.created_at DESC LIMIT 200",
                        (self.user_id, self.namespace),
                    ).fetchall()
                    seen = set()
                    for r in mrows:
                        text = (r["content"] or "").strip()
                        if not text or text in seen:
                            continue
                        base = _score(text)
                        if base > 0:
                            seen.add(text)
                            results.append({"kind": "message", "text": text,
                                            "score": base * 0.7, "category": "其他"})
                except Exception:
                    pass

            conn.close()
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:limit]

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"主动回忆检索失败: {e}")
            return []

    async def build_recall_block(self, query: str) -> Optional[str]:
        """构建按需注入的『相关回忆』文本片段（预算受限，防止上下文膨胀）。

        分层打包开启时，除字符上限外额外受 recall_max_tokens（token 预算）约束，
        两者取更严格者强制生效。无相关内容时返回 None（调用方不注入额外消息）。
        """
        cfg = self._recall_settings()
        if not cfg["recall_enabled"]:
            return None
        items = await self.recall_memories(query)
        if not items:
            return None
        hcfg = self._hierarchical_settings()
        token_budget = hcfg['recall_max_tokens'] if hcfg['hierarchical_packing'] else 0
        lines, _ = _format_recall_lines(items, cfg["recall_max_chars"], token_budget)
        if not lines:
            return None
        head = "【相关回忆·按需注入】根据当前话题检索到的相关记忆，供你自然引用（无需刻意复述）：\n"
        return head + "\n".join(lines)

    # ==================== 分层记忆上下文打包（token 预算） ====================

    async def get_recent_work_digest(self, max_chars: int = None) -> Optional[str]:
        """最近一次「带工具执行的用户轮次」的真实工具结果摘要。

        修复"刚做完的事转头就忘"：连接级历史只保留每轮最终回复，工具轮的
        真实执行结果只存在于消息库；本方法把最近一次带工具轮次的
        「目标 + 实际调用的工具 + 截断后的真实结果」原样取回，
        每轮注入上下文，模型不再需要靠重读文件来回忆自己做过什么。

        从最近 8 个用户轮次中从新到旧找第一个含工具结果的轮次；
        没有工具轮时返回 None（不注入，保持上下文精简）。

        Returns:
            带标题的工具结果文本块，或 None。
        """
        if not self.session_id:
            return None
        max_chars = max_chars or RECENT_WORK_MAX_CHARS

        def _do():
            conn = _get_db()
            rows = conn.execute(
                "SELECT role, content, tool_calls FROM messages "
                "WHERE session_id=? AND (source != 'auto' OR role != 'user') "
                "ORDER BY id DESC LIMIT 120",
                (self.session_id,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows][::-1]  # 旧→新

        rows = await asyncio.to_thread(_do)
        if not rows:
            return None

        # 按用户消息切轮次（与 _group_history_rounds 同语义）
        rounds = []
        cur = None
        for m in rows:
            if m.get("role") == "user":
                cur = [m]
                rounds.append(cur)
            elif cur is not None:
                cur.append(m)
            else:
                cur = [m]
                rounds.append(cur)

        for rnd in reversed(rounds[-8:]):
            tool_msgs = [m for m in rnd if m.get("role") == "tool"]
            if not tool_msgs:
                continue
            names = []
            for m in rnd:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    try:
                        tcs = json.loads(m["tool_calls"])
                        names += [str((tc.get("function") or {}).get("name") or "?")
                                  for tc in tcs]
                    except (json.JSONDecodeError, TypeError):
                        pass
            goal = next((str(m.get("content") or "") for m in rnd
                         if m.get("role") == "user"), "")
            goal = goal.strip().replace("\n", " ")
            if len(goal) > 120:
                goal = goal[:117] + "…"
            digest = []
            for i, m in enumerate(tool_msgs[:8]):
                nm = names[i] if i < len(names) else "工具"
                c = (str(m.get("content") or "").strip()
                     .replace("\r", " ").replace("\n", " "))
                if not c:
                    continue
                if len(c) > 160:
                    c = c[:157] + "…"
                digest.append(f"- {nm}: {c}")
            if not digest:
                continue
            block = ("【最近一次任务执行（真实工具结果，请以此为准，不必重复读取）】"
                     f"目标：{goal}\n" + "\n".join(digest))
            if len(block) > max_chars:
                block = block[:max_chars] + "…"
            return block
        return None

    async def build_hierarchical_context(self, query: str = None,
                                         connection_history: list = None,
                                         recent_n: int = None) -> dict:
        """打包本轮 LLM 的记忆类上下文：短期窗口 + 长期摘要 + 常驻长期记忆 + 按需召回。

        四层独立受 token 预算约束（预算参数见 _hierarchical_settings）：
        - 短期窗口：最近轮次，新→旧挑选直到满 short_term_max_tokens，单轮超长截到
          short_term_max_chars_per_round（历史天然\"记住最近\"）；
        - 长期摘要：只带最新 summary_max_tokens 的增量摘要；
        - 长期关键信息：最高 importance 的 long_term_top_k 条常驻记忆；
        - 按需召回：检索出的相关片段，recall_max_tokens 强制封顶。

        hierarchical_packing=false 时完全回退到旧行为（各层不设 token 上限），
        agent.py 组装逻辑不变，实现一键软回滚。

        Args:
            query: 当前用户消息（主动回忆的检索词）
            connection_history: 服务端连接级历史 [{"user","ai"}...]（旧→新）；
                None 时从记忆库读取最近消息作为短期窗口来源。
            recent_n: 从记忆库读取的最近消息数上限（默认 RECENT_MESSAGE_COUNT）。

        Returns:
            {
              "summary_block": 摘要块文本或 None,
              "memory_block": 常驻长期记忆块文本或 None,
              "recall_block": 主动回忆块文本或 None,
              "work_block": 最近任务执行（真实工具结果）块文本或 None,
              "history": 打包后的历史消息列表（旧→新，含 tool 结构）,
              "stats": {"raw_tokens","packed_tokens","history_rounds",
                        "summaries","memories","recall_items"} 或 None（record_stats=false）
            }
        """
        if not self.session_id:
            return {"summary_block": None, "memory_block": None, "recall_block": None,
                    "work_block": None,
                    "history": [], "stats": None}
        # 防过期摘要：未摘要区间里若有一轮"带工具执行/完成汇报且已收尾"的对话
        # （例如任务刚做完、还没到固定摘要间隔），先补一次摘要再组装上下文。
        # 否则后续主动对话会读到过期的"进行中"摘要，与刚完成的执行结果自相矛盾。
        try:
            if await self._has_unsaved_work_round():
                await self.force_summarize(
                    include_auto=True, auto_prefix=("【子智能体汇报",))
        except Exception as e:
            logger.warning(f"上下文构建前补摘要失败（忽略）: {e}")
        hcfg = self._hierarchical_settings()
        enabled = hcfg["hierarchical_packing"]
        record = bool(hcfg["record_stats"])
        # DB 主源路径需要更多消息才能覆盖「最近几轮 + 被打断轮的工具轮」，
        # 单轮工具循环可占 60+ 条消息（见 build_hierarchical_context 的说明）
        recent_n = recent_n or max(RECENT_MESSAGE_COUNT, 200)

        def _read():
            conn = _get_db()
            srows = conn.execute(
                "SELECT summary_text FROM summaries WHERE session_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (self.session_id, MAX_SUMMARIES),
            ).fetchall()
            mrows = conn.execute(
                "SELECT role, content, tool_calls, tool_results FROM messages "
                "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
                "ORDER BY id DESC LIMIT ?",
                (self.session_id, max(400, recent_n * 2)),
            ).fetchall()[::-1]  # 反转为旧→新，给打包器足够挑选
            mem_rows = conn.execute(
                "SELECT memory_text FROM user_memories WHERE user_id=? AND namespace=? "
                "ORDER BY importance DESC, id DESC LIMIT ?",
                (self.user_id, self.namespace, max(hcfg['long_term_top_k'], 8)),
            ).fetchall()
            conn.close()
            return srows, mrows, mem_rows

        srows, mrows, mem_rows = await asyncio.to_thread(_read)

        # ---- 摘要层 ----
        summary_texts = [_clean_summary_noise(r["summary_text"]) for r in srows]
        if enabled:
            kept_s, _ = _pack_by_token_budget(
                summary_texts, hcfg['summary_max_tokens'], hcfg['summary_max_chars_per_item'])
            n_summaries = len(kept_s)
            summary_block = ("以下是之前的对话摘要：\n" + "；".join(kept_s)) if kept_s else None
        else:
            combined = "；".join(summary_texts)
            if len(combined) > 600:
                combined = combined[:597] + "..."
            n_summaries = len(summary_texts)
            summary_block = ("以下是之前的对话摘要：\n" + combined) if combined else None

        # ---- 常驻长期记忆层 ----
        mem_lines = [f"- {m['memory_text']}" for m in mem_rows[:hcfg['long_term_top_k']]]
        if enabled and mem_lines:
            kept_m, _ = _pack_by_token_budget(mem_lines, hcfg['long_term_max_tokens'])
            mem_lines = kept_m
        n_memories = len(mem_lines)
        memory_block = ("【关于用户的长期记忆（你之前了解到的用户信息，"
                        "请自然地在对话中体现）】\n" + "\n".join(mem_lines)) if mem_lines else None

        # ---- 按需召回层 ----
        recall_lines, recall_items_count = [], 0
        cfg = self._recall_settings()
        if cfg["recall_enabled"] and query and len(query.strip()) >= 2:
            items = await self.recall_memories(query)
            if items:
                recall_lines, recall_items_count = _format_recall_lines(
                    items, cfg["recall_max_chars"],
                    hcfg['recall_max_tokens'] if enabled else 0)
        head = ("【相关回忆·按需注入】根据当前话题检索到的相关记忆，"
                "供你自然引用（无需刻意复述）：\n") if recall_lines else None
        recall_block = head + "\n".join(recall_lines) if recall_lines else None

        # ---- 最近任务执行层（真实工具结果，动态尾巴） ----
        work_block = None
        try:
            work_block = await self.get_recent_work_digest()
        except Exception as e:
            logger.warning(f"注入最近任务执行结果失败（忽略）: {e}")

        # ---- 短期窗口 ----
        # 2026-08-30 修复「打断丢上下文」：主源改用消息库（含工具轮真实结果）。
        # 连接级历史只存每轮最终回复，被打断的轮次没有最终回复 → 下一轮只剩
        # 「用户:xxx / AI:(空)」，正在做的工具工作全部丢失。改从 DB 取最近
        # 消息后，打断现场的工具执行会原样进入下一轮上下文。
        if connection_history is not None and not mrows:
            raw_records = _connection_pairs_to_records(connection_history)
        else:
            # DB 的 tool_calls/tool_results 是 JSON 字符串，必须反序列化为 list，
            # 否则打包后原样进 LLM 请求，sglang 等提供方会以 400 拒绝
            # （tool_calls 应为 list 却收到 str）。
            def _parse_json_field(v):
                if v is None:
                    return None
                if isinstance(v, str):
                    try:
                        return json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        return None
                return v

            raw_records = [
                {"role": r["role"], "content": r["content"],
                 "tool_calls": _parse_json_field(r["tool_calls"]),
                 "tool_results": _parse_json_field(r["tool_results"])}
                for r in mrows[-recent_n:]] if enabled else [
                {"role": r["role"], "content": r["content"]}
                for r in mrows[-recent_n:]]
        if enabled:
            history = _pack_history_records(
                raw_records, hcfg['short_term_max_tokens'],
                hcfg['short_term_max_chars_per_round'],
                min_rounds=hcfg.get('short_term_min_rounds', SHORT_TERM_MIN_ROUNDS),
                max_chars_per_tool=hcfg.get('short_term_max_chars_per_tool',
                                            SHORT_TERM_MAX_CHARS_PER_TOOL),
                max_chars_per_tool_call=hcfg.get(
                    'short_term_max_chars_per_tool_call',
                    SHORT_TERM_MAX_CHARS_PER_TOOL_CALL),
                keep_last_tools=hcfg.get('short_term_keep_last_tools',
                                         SHORT_TERM_KEEP_LAST_TOOLS))
        else:
            history = raw_records
        n_rounds = sum(1 for m in history if m.get("role") == "user")

        # ---- 量化统计（旧规则 vs 分层预算）----
        if not record:
            stats = None
        else:
            # 旧规则基线：完整短期窗口 + 3 条摘要合并 600 字 + top-2 记忆 + 召回 800 字
            def _legacy_blocks():
                s_legacy = ("以下是之前的对话摘要：\n" + "；".join(summary_texts[:MAX_SUMMARIES]))[:600]
                m_legacy = "；".join(f"- {m['memory_text']}" for m in mem_rows[:2])
                r_legacy = ""
                if recall_lines:
                    r_legacy = head + "\n".join(recall_lines)
                return s_legacy, m_legacy, r_legacy

            s_legacy, m_legacy, r_legacy = _legacy_blocks()
            raw_tokens = (estimate_tokens(s_legacy) + estimate_tokens(m_legacy)
                          + estimate_tokens(r_legacy)
                          + sum(estimate_tokens(m.get('content') or '')
                                for m in raw_records))
            packed_tokens = (estimate_tokens(summary_block or '')
                             + estimate_tokens(memory_block or '')
                             + estimate_tokens(recall_block or '')
                             + estimate_tokens(work_block or '')
                             + sum(estimate_tokens(m.get('content') or '')
                                   for m in history))
            stats = {
                "raw_tokens": raw_tokens,
                "packed_tokens": packed_tokens,
                "history_rounds": n_rounds,
                "summaries": n_summaries,
                "memories": n_memories,
                "recall_items": recall_items_count,
            }

        return {"summary_block": summary_block, "memory_block": memory_block,
                "recall_block": recall_block, "work_block": work_block,
                "history": history, "stats": stats}

    async def record_context_stats(self, stats: dict, actual_prompt_tokens: int = 0):
        """写入一轮对话的上下文用量（context_stats 表），用于量化 token 节省。"""
        if not stats:
            return
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute(
                "INSERT INTO context_stats (session_id, namespace, raw_tokens, packed_tokens, "
                "actual_prompt_tokens, history_rounds, summaries, memories, recall_items, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.session_id, self.namespace, stats.get("raw_tokens", 0),
                 stats.get("packed_tokens", 0), actual_prompt_tokens,
                 stats.get("history_rounds", 0), stats.get("summaries", 0),
                 stats.get("memories", 0), stats.get("recall_items", 0), time.time()),
            )
            conn.commit()
        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning(f"记录 context_stats 失败: {e}")

    # ==================== 清理 ====================

    async def close_session(self):
        """关闭当前会话（标记为非活跃）。"""
        if not self.session_id:
            return
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute(
                "UPDATE sessions SET is_active=0, updated_at=? WHERE id=?",
                (time.time(), self.session_id),
            )
            conn.commit()
        await asyncio.to_thread(_do)
        self.session_id = None
        self._message_count = 0

    async def delete_session(self, session_id: str = None):
        """删除指定会话及其所有消息。"""
        sid = session_id or self.session_id
        if not sid:
            return
        @_with_db_lock
        def _do():
            conn = _get_db()
            conn.execute("DELETE FROM summaries WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            conn.commit()
        await asyncio.to_thread(_do)
        if sid == self.session_id:
            self.session_id = None
            self._message_count = 0
