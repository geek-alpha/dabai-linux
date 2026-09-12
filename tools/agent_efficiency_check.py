# -*- coding: utf-8 -*-
"""主智能体效率检查：上下文压缩等价性 + token 估算一致性 + 分批调度。

为什么必须有「等价性」测试：
  把 O(轮数 × 消息数) 的重复全量扫描改成增量维护，是纯性能优化——
  前提是**结果一个字都不能变**。没有对照测试，这种改动就只能靠感觉，
  而感觉在「压缩预算」这种事上从来不可靠。

用法：python tools/agent_efficiency_check.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 诊断日志打上 test 标记：自检造的合成样本绝不能混进真实运行观测，
# 否则 data/*_trace.jsonl 里分不出哪条是现场、哪条是测试。
os.environ["DABAI_TRACE_TAG"] = "test"

import asyncio  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402

import agent as A  # noqa: E402
from memory import estimate_tokens  # noqa: E402

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name
          + (("  ← " + detail) if detail else ""))


print("=== 主智能体效率检查 ===")

# ============================================================
#  一、estimate_tokens：预编译不能改数值
# ============================================================
print("\n[1] estimate_tokens 数值一致性")


def _old_estimate(text):
    """旧实现逐行复刻：函数内 import + 未预编译正则。"""
    if not text:
        return 0
    s = str(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", s))
    rest = re.sub(r"[\u4e00-\u9fff\u3400-\u4dbf]", "", s)
    words = re.findall(r"[A-Za-z0-9]+", rest)
    rest_no_words = re.sub(r"[A-Za-z0-9]+", "", rest)
    punct = len(re.sub(r"\s", "", rest_no_words))
    est = cjk + sum((len(w) + 3) // 4 for w in words) + (punct + 1) // 2
    return int(est * 1.1) + 1


samples = [
    "", "hello world", "中文测试", "混合 hello 世界 123",
    "代码：def foo(x): return x * 2  # 注释",
    "a" * 500, "中" * 500, "emoji 与标点，。！？",
    "tool result:\n" + "x = 1\n" * 100,
    "  空白\n\t制表  ",
]
mismatch = [s[:14] for s in samples if estimate_tokens(s) != _old_estimate(s)]
check("10 组样本数值与旧公式完全一致（历史校准数据不作废）",
      not mismatch, "不一致：%s" % mismatch if mismatch else "全部一致")

# ============================================================
#  二、上下文压缩：与旧逻辑逐条对照
# ============================================================
print("\n[2] _compact_tool_history 等价性（增量维护不能改结果）")


def _naive_compact(messages, budget, keep_rounds, retro_cap):
    """旧实现复刻：每个旧轮压完都全量重算 token。"""
    def est(m):
        return estimate_tokens(str(m.get("content") or ""))

    if sum(est(m) for m in messages) <= budget:
        over_budget = False
    else:
        over_budget = True
    single_cap = A._single_result_max_tokens()

    def is_result(m):
        return (m.get("role") == "tool"
                or (m.get("role") == "system"
                    and str(m.get("content") or "").startswith("【工具")))

    n = len(messages)
    groups = []
    i = 0
    while i < n:
        if messages[i].get("role") == "assistant":
            j = i + 1
            while j < n and is_result(messages[j]):
                j += 1
            groups.append((i, j, j > i + 1))
            i = j
        else:
            i += 1
    tool_groups = [g for g in groups if g[2]]
    if not tool_groups:
        return messages

    old_groups = list(tool_groups[:-keep_rounds] if keep_rounds else [])
    for start, end, _ in reversed(old_groups):
        for k in range(start, end):
            m = messages[k]
            if is_result(m):
                c = str(m.get("content") or "")
                if (over_budget or est(m) > single_cap) and len(c) > retro_cap:
                    m["content"] = c[:retro_cap] + "…【已压缩】"
            elif m.get("role") == "assistant" and over_budget:
                c = str(m.get("content") or "")
                if len(c) > A.ASSISTANT_RETRO_CAP:
                    m["content"] = c[:A.ASSISTANT_RETRO_CAP] + "…"
        if over_budget and sum(est(m) for m in messages) <= budget:
            break

    if sum(est(m) for m in messages) > budget and tool_groups:
        for cap in (2000, 600):
            start, end, _ = tool_groups[-1]
            for k in range(start, end):
                m = messages[k]
                if is_result(m):
                    c = str(m.get("content") or "")
                    if len(c) > cap:
                        m["content"] = c[:cap] + "…【已压缩】"
            if sum(est(m) for m in messages) <= budget:
                break
    return messages


def build_history(rounds, result_len, text_protocol=False):
    msgs = [{"role": "system", "content": "SYS " * 20}]
    for i in range(rounds):
        if text_protocol:
            msgs.append({"role": "assistant", "content": "<tool_call>code_read</tool_call>"})
            msgs.append({"role": "system",
                         "content": "【工具 code_read】\n" + "T" * result_len})
        else:
            msgs.append({"role": "assistant", "content": "思考 %d" % i,
                         "tool_calls": [{"id": "c%d" % i,
                                         "function": {"name": "code_read"}}]})
            msgs.append({"role": "tool", "tool_call_id": "c%d" % i,
                         "content": "R" * result_len})
    return msgs


CASES = (
    ("未超预算（应逐字不动）", 20, 300, 10 ** 9, False),
    ("单条超大·总量未超（新触发线）", 3, 40000, 10 ** 9, False),
    ("轻度超预算", 40, 2000, 30000, False),
    ("极端超预算（触发 600 字兜底）", 60, 8000, 8000, False),
    ("文本协议【工具】消息", 30, 4000, 20000, True),
)

for label, rounds, rlen, budget, tp in CASES:
    a = build_history(rounds, rlen, tp)
    b = build_history(rounds, rlen, tp)
    A._compact_tool_history(a, budget=budget, keep_rounds=1)
    _naive_compact(b, budget, 1, A._tool_result_retro_cap())
    diff = [i for i in range(len(a)) if a[i].get("content") != b[i].get("content")]
    check("%s：压缩结果与旧逻辑逐条一致" % label, not diff,
          "差异 %d 条 %s" % (len(diff), diff[:5]) if diff else "")
    check("%s：条数与 role 未变" % label,
          len(a) == len(b)
          and all(a[i].get("role") == b[i].get("role") for i in range(len(a))))
    check("%s：tool_call_id 结构未破坏" % label,
          all(a[i].get("tool_call_id") == b[i].get("tool_call_id")
              for i in range(len(a))))

# 新触发线的「防假通过」断言：两边一致还不够——若两边都没压，一致也没意义。
# 必须同时确认：旧轮的超大结果真被压了，且最新轮（模型正在用）没被碰。
big = build_history(3, 40000)
A._compact_tool_history(big, budget=10 ** 9, keep_rounds=1)
# 注意下标：build_history 是 [system, assistant, tool, assistant, tool, ...]，
# 旧轮的 tool 结果不是 big[1]（那是 assistant），得按 role 挑。
old_results = [i for i in range(len(big)) if big[i].get("role") == "tool"][:-1]
check("单条超大·总量未超：旧轮结果确实被压",
      old_results and all("已压缩" in str(big[i].get("content")) for i in old_results),
      "旧轮下标 %s，长度 %s" % (old_results,
                              [len(str(big[i].get("content"))) for i in old_results]))
check("单条超大·总量未超：最新轮保持完整",
      "已压缩" not in str(big[-1].get("content")),
      "len=%d" % len(str(big[-1].get("content"))))

small = build_history(20, 300)
A._compact_tool_history(small, budget=10 ** 9, keep_rounds=1)
check("单条不超大·总量未超：仍逐字节不动",
      all("已压缩" not in str(m.get("content")) for m in small))

# ============================================================
#  三、性能：不再随轮数平方增长
# ============================================================
print("\n[3] 压缩性能（旧实现是 O(轮数 × 消息数)）")

a = build_history(60, 8000)
t0 = time.perf_counter()
A._compact_tool_history(a, budget=8000, keep_rounds=1)
new_ms = (time.perf_counter() - t0) * 1000

b = build_history(60, 8000)
t0 = time.perf_counter()
_naive_compact(b, 8000, 1, A._tool_result_retro_cap())
old_ms = (time.perf_counter() - t0) * 1000

check("增量维护比全量重算快", new_ms < old_ms,
      "新 %.1fms vs 旧 %.1fms（提速 %.0f×）" % (new_ms, old_ms, old_ms / max(new_ms, 0.01)))
check("长历史上不阻塞事件循环（< 100ms）", new_ms < 100, "%.1fms" % new_ms)

# 未超预算时必须零改动（前缀缓存友好）
c = build_history(30, 500)
snapshot = [dict(m) for m in c]
A._compact_tool_history(c, budget=10 ** 9, keep_rounds=1)
check("未超预算时逐字节不变（不破坏前缀缓存）",
      all(c[i].get("content") == snapshot[i].get("content") for i in range(len(c))))

# ============================================================
#  四、分批调度
# ============================================================
print("\n[4] 分批调度（只读不被写拖累）")

b = A._plan_batches([(0, "code_read", {"path": "a.py"}),
                     (1, "shell_run", {"command": "pytest"})])
check("只读与 shell_run 分属不同批", bool(b) and len(b) == 2,
      "批数=%d" % (len(b) if b else 0))
check("只读排在第一批（先读后写，状态确定）",
      bool(b) and all(x[1] == "code_read" for x in b[0]))
check("空输入返回 None（调用方退回串行，不抛异常）", A._plan_batches([]) is None)

# ============================================================
#  五、断点落盘：紧凑序列化不能改变可读回性
# ============================================================
print("\n[5] 断点落盘（紧凑 JSON + 线程化）")

TEST_USER = "__effcheck__"


def _cleanup_ckpt():
    for p in A.TURN_CKPT_DIR.glob(TEST_USER + "*"):
        try:
            p.unlink()
        except Exception:
            pass


_cleanup_ckpt()

cp = {"version": 1, "turn_id": "effcheck1", "user_id": TEST_USER,
      "user_message": "测试", "messages": build_history(5, 200),
      "history": [], "tool_round": 2, "pending_tools": [],
      "assistant_content": "思考中", "memory_anchor": 7}

A.save_turn_checkpoint(TEST_USER, cp)
back = A.load_turn_checkpoint(TEST_USER)
check("紧凑序列化后仍能完整读回（内容逐字一致）",
      bool(back) and back.get("messages") == cp["messages"],
      "messages=%d" % (len(back.get("messages") or []) if back else -1))
check("读回的字段与写入一致（除 updated_at 等时间戳）",
      bool(back) and all(back.get(k) == cp[k]
                         for k in ("turn_id", "user_message", "tool_round",
                                   "assistant_content", "memory_anchor")))

slot = A._turn_ckpt_slot_path(TEST_USER, "effcheck1")
raw = slot.read_text(encoding="utf-8")
check("落盘是紧凑格式（无缩进换行，体积更小）",
      "\n  " not in raw and raw.count("\n") == 0, "%d 字节" % len(raw))
pretty = json.dumps(cp, ensure_ascii=False, indent=2)
check("紧凑格式比 indent=2 小（省磁盘 IO）", len(raw) < len(pretty),
      "%d vs %d 字节（省 %.0f%%）" % (len(raw), len(pretty),
                                     (1 - len(raw) / len(pretty)) * 100))


async def _thread_save_case():
    """线程化落盘：连续两次保存必须有序、且不丢最后一次内容。"""
    for i in (1, 2, 3):
        c = dict(cp, turn_id="effcheck2", assistant_content="第%d次" % i,
                 messages=build_history(3, 100))
        await asyncio.to_thread(A.save_turn_checkpoint, TEST_USER, c)
    return A.load_turn_checkpoint(TEST_USER)


back2 = asyncio.run(_thread_save_case())
check("线程化落盘后读回的是最后一次内容（顺序未被并发打乱）",
      bool(back2) and back2.get("assistant_content") == "第3次",
      str(back2.get("assistant_content") if back2 else None))

_cleanup_ckpt()
check("测试清理干净（未残留断点文件）",
      not list(A.TURN_CKPT_DIR.glob(TEST_USER + "*")))

# ============================================================
#  六、重复调用提示：只给 LLM，不污染记忆与前端
# ============================================================
print("\n[6] 重复调用提示")

check("提示常量存在且足够短（不撑大上下文）",
      0 < len(A.REPEAT_CALL_HINT) < 200, "%d 字" % len(A.REPEAT_CALL_HINT))
check("提示内容明确说了「结果不会变化」（否则模型不会停止重试）",
      "不变" in A.REPEAT_CALL_HINT or "一致" in A.REPEAT_CALL_HINT)

fp1 = A._tool_fp("code_read", {"path": "a.py", "start": 1})
fp2 = A._tool_fp("code_read", {"start": 1, "path": "a.py"})
check("参数顺序不同视为同一次调用（指纹按 key 排序）", fp1 == fp2, fp1)
check("参数不同视为不同调用",
      A._tool_fp("code_read", {"path": "b.py"}) != fp1)
check("工具名不同视为不同调用",
      A._tool_fp("code_search", {"path": "a.py", "start": 1}) != fp1)

# ============================================================
#  七、短期窗口滞回：让「每轮失效」变成「每 N 轮失效一次」
#
#  实测（tools/cold_audit.py，12 轮）：6 轮断点在 history[0]，可复用仅 2.4k 字符
#  （sys + 常驻记忆），作废 51k~324k 字符——整段历史每轮白付一次全价。
#  根因：ctx 的 history 由 memory 每轮按预算「新→旧重挑」，预算一满最旧一轮滑出。
#  下面每一项都对应一个会让它「悄悄退回原状」的具体写法。
# ============================================================
print("\n[7] 短期窗口滞回（_stable_history_messages）")


class _StubMem:
    def __init__(self, sid):
        self.session_id = sid


def _stub(sid="s1"):
    a = A.AIAgent.__new__(A.AIAgent)      # 不跑 __init__：这些检查只碰纯函数逻辑
    a.memory = _StubMem(sid)
    return a


def _u(text):
    return {"role": "user", "content": text}


def _a(text):
    return {"role": "assistant", "content": text}


ag = _stub()
p1 = [_u("问题1"), _a("回答1"), _u("问题2"), _a("回答2")]
v1 = A.AIAgent._stable_history_messages(ag, p1)
check("首轮：视图 = 打包结果", v1 == p1)

p2 = p1 + [_u("问题3"), _a("回答3")]
v2 = A.AIAgent._stable_history_messages(ag, p2)
check("只追加新轮：前缀逐字节不变（整段历史命中缓存）",
      v2[:len(v1)] == v1 and len(v2) == len(p2),
      "v1=%d 条，v2=%d 条" % (len(v1), len(v2)))

# 关键场景：预算满 → 打包把最旧一轮从头部滑掉
p3 = p2[2:] + [_u("问题4"), _a("回答4")]
v3 = A.AIAgent._stable_history_messages(ag, p3)
check("★ 打包从旧端滑掉一轮：视图仍保住旧轮（前缀不失效）",
      v3[:len(v2)] == v2 and len(v3) == len(v2) + 2,
      "上轮 %d 条 → 本轮 %d 条" % (len(v2), len(v3)))

# 关键场景：同一轮「由新变旧」后被字符上限截断，全文必然不同
p4 = v3 + [_u("问题5"), _a("A" * 500)]
v4 = A.AIAgent._stable_history_messages(ag, p4)
p5 = [dict(m) for m in v4]
p5[-1] = _a("A" * 100)                      # 变旧轮 → 被截到 100 字符
p5 += [_u("问题6"), _a("回答6")]
v5 = A.AIAgent._stable_history_messages(ag, p5)
check("★ 锚点抗截断：旧轮被截短仍能锚定，不误判成换会话",
      v5[:len(v4)] == v4 and len(v5) == len(v4) + 2,
      "被截的是第 %d 条" % (len(v4) - 1))

ag2 = _stub("s2")
v6 = A.AIAgent._stable_history_messages(ag2, p5)
check("换会话：不带上一会话的视图（防串会话）", v6 == p5)

p7 = [_u("完全无关"), _a("X")]
v7 = A.AIAgent._stable_history_messages(ag, p7)
check("锚点找不到（历史被改写）→ 重建，不拼接无关内容", v7 == p7)

# ============================================================
#  八、历史视图跨进程持久化：重启不该等于「换会话」
#
#  实测（data/turn_metrics.jsonl × data/hist_view_trace.jsonl，46 轮）：
#  instance_new → reset_sid 重建之后的头几轮，断点全落在第 1~3 条，
#  单轮白烧 68k~324k 字符；而视图连续的轮次断点稳定在 history[45~53]，
#  只废 2k~16k——差两个数量级。重建的新头部由打包器决定，而打包器每轮
#  按 token 预算重挑、落点会变，所以「丢视图」= 后面几轮都要重付全价。
#  下面每一项都对应一个会让它「悄悄退回原状」的具体写法。
# ============================================================
print("\n[8] 历史视图跨进程持久化（_load_hist_view / _save_hist_view）")

_tmpdir = tempfile.mkdtemp(prefix="histview_")
_real_path = A.HIST_VIEW_STATE_PATH
_real_enabled = A._hist_view_persist_enabled
A.HIST_VIEW_STATE_PATH = os.path.join(_tmpdir, "hist_view_state.json")
A._hist_view_persist_enabled = lambda: True          # 临时放开 tag=test 的闸门

A._save_hist_view("s1", p1)
check("存盘后能按同 sid 读回", A._load_hist_view("s1") == p1)
check("★ 换 sid 不接旧视图（防跨会话串上下文）", A._load_hist_view("s9") == [])
check("空 sid 直接当没有视图", A._load_hist_view("") == [])

# 真·重启场景：新实例（无 _hist_msgs_view）、同 sid
ag3 = _stub("s1")
v8 = A.AIAgent._stable_history_messages(ag3, p2)
check("★ 进程重启后视图接回而不是重建（同 sid）",
      v8[:len(p1)] == p1 and len(v8) == len(p2),
      "重启后视图 %d 条，期望前 %d 条与旧视图逐字节一致" % (len(v8), len(p1)))

A._save_hist_view("s1", None)
check("视图为空时不落盘垃圾", A._load_hist_view("s1") == [])

with open(A.HIST_VIEW_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write("{坏 JSON")
check("★ 状态文件损坏 → 降级为「没有视图」，绝不抛异常",
      A._load_hist_view("s1") == [])

with open(A.HIST_VIEW_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write(json.dumps({"sid": "s1", "view": "不是列表"}))
check("view 类型不对 → 当作没有视图（不猜）", A._load_hist_view("s1") == [])

with open(A.HIST_VIEW_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write(json.dumps({"sid": "s1", "view": [1, "x", {"role": "user"}]}))
check("非 dict 元素被过滤，只剩合法消息",
      A._load_hist_view("s1") == [{"role": "user"}])

# 恢复真实配置：自检必须关闸，否则合成样本会写进现场状态文件
A.HIST_VIEW_STATE_PATH = _real_path
A._hist_view_persist_enabled = _real_enabled
check("★ 自检(tag=test)不读写真实现场状态文件",
      A._hist_view_persist_enabled() is False)

big = []
for _i in range(40):
    big.append(_u("问题%d" % _i))
    big.append(_a("回答" * 200))
vbig = A.AIAgent._stable_history_messages(ag, big)
check("超上限才截断，且截断后不超上限",
      A._hist_view_tokens(vbig) <= A.HIST_VIEW_MAX_TOKENS,
      "%d token / 上限 %d" % (A._hist_view_tokens(vbig), A.HIST_VIEW_MAX_TOKENS))
# 只验「不超上限」是不够的：一刀砍到只剩一轮也能过。必须验「预算用满了」——
# 倒序找切点的写法正是这样漏掉的（第一个满足条件的总是最新那个 user）。
check("★ 截断是「裁到预算内」而不是「一刀砍到只剩一轮」",
      A._hist_view_tokens(vbig) > A.HIST_VIEW_MAX_TOKENS // 4,
      "%d token / 上限 %d" % (A._hist_view_tokens(vbig), A.HIST_VIEW_MAX_TOKENS))
check("截断切点落在 user 消息上（首条不是 tool，无悬空引用）",
      bool(vbig) and vbig[0].get("role") == "user", str(vbig[0].get("role")))

tc = []
for _i in range(30):
    tc.append(_u("任务%d" % _i))
    tc.append({"role": "assistant", "content": "调用工具",
               "tool_calls": [{"id": "c%d" % _i,
                               "function": {"name": "code_read", "arguments": "{}"}}]})
    tc.append({"role": "tool", "content": "结果" * 300})
vt = A.AIAgent._stable_history_messages(ag, tc)
check("截断后每个 tool 结果前面都有 tool_calls（function-calling 校验通过）",
      all(any(m.get("tool_calls") for m in vt[:_i])
          for _i, m in enumerate(vt) if m.get("role") == "tool"))

# 空打包 ≠ 会话结束：这是真实运行里 93% 的前缀断裂来源。
# 实测 data/hist_view_trace.jsonl（257 条真实运行样本）：15 次 anchor_lost 里
# 14 次紧跟一次 empty —— 旧写法在 packed 空时把视图清掉，下一轮 packed 恢复
# 就无锚点可对，只能全量重建，把整段历史按全价重发一遍。
ag_e = _stub("s_empty")
pe = [_u("问题1"), _a("回答1"), _u("问题2"), _a("回答2")]
check("空包场景准备：视图已建立",
      A.AIAgent._stable_history_messages(ag_e, pe) == pe)
# ⚠ 这一条必须打在被断言的那个实例上：早先写成 ag，于是 ag_e 从未经历
# 「空包」，后面那条「视图仍在」永远为真——退回旧写法做反向验证时全绿，
# 是个假通过（验证器自己失效比 bug 更危险）。
check("空打包结果：返回空（不往请求里塞历史）",
      A.AIAgent._stable_history_messages(ag_e, []) == [])
check("★★ 空打包后视图仍在（清掉它 = 下一轮必然全量重建）",
      getattr(ag_e, "_hist_msgs_view", None) == pe,
      "视图 %d 条 / 期望保留 %d 条"
      % (len(getattr(ag_e, "_hist_msgs_view", []) or []), len(pe)))

# 下一轮打包恢复，且比视图短（打包器缩水是常态，不是异常）
pe_next = pe[-2:] + [_u("问题3"), _a("回答3")]
ve1 = A.AIAgent._stable_history_messages(ag_e, pe_next)
check("★★ 空包后的下一轮走追加而非重建（前缀逐字节保住）",
      ve1[:len(pe)] == pe and len(ve1) == len(pe) + 2,
      "视图 %d 条，期望前 %d 条与空包前一致" % (len(ve1), len(pe)))

# 反向守卫：不能把「空包保留」写成「永远保留」——真换会话必须清。
ag_sw = _stub("s_switch")
A.AIAgent._stable_history_messages(ag_sw, pe)
ag_sw.memory.session_id = "s_other"
check("★ 换会话时空包仍清视图（保留策略不越界到跳会话）",
      A.AIAgent._stable_history_messages(ag_sw, []) == []
      and getattr(ag_sw, "_hist_msgs_view", None) == [])

# 死代码守卫：上一次的滞回窗口只定义、只在回退路径调用，主路径照样每轮失效。
# 这条检查盯着「接线」，因为「定义存在」不等于「生效」。
_src_a = open(A.__file__, encoding="utf-8").read()
check("★★ ctx 主路径已接入滞回视图（不是只定义不调用）",
      'messages.extend(self._stable_history_messages(ctx.get("history") or []))' in _src_a)

# ============================================================
# ============================================================
#  八、工具集稳定：tools 排在请求最前，它一动整条前缀全废
#  实测（data/turn_metrics.jsonl，16 轮）：工具集在 48 ↔ 1 之间跳了 7 次，
#  每次跳都把整条前缀（含全部历史）废掉 200k+ 字符（全价计费）。
#  根因不是「激活了什么」，而是上限正好卡在 1（skill_help）+ 47（code_ops）= 48：
#  只要再激活任何一个技能，LRU 就把 code_ops 的 47 个工具整段删掉。
#  下面每一项都盯着一个「会让它悄悄退回原状」的具体写法。
# ============================================================
import harness as _H  # noqa: E402


class _FakeHarness:
    """假 harness：技能 → 工具名清单，不起服务就能验注册/淘汰/顺序。"""

    def __init__(self, table):
        self.table = table

    def skill_tool_specs(self, name):
        return [{"type": "function",
                 "function": {"name": n, "description": n,
                              "parameters": {"type": "object", "properties": {}}}}
                for n in self.table.get(name, [])]


_TABLE = {"code_ops": ["code_%d" % i for i in range(47)],
          "media": ["media_%d" % i for i in range(34)],
          "tasks": ["task_%d" % i for i in range(23)]}


def _mk_agent(max_tools=200):
    """造一个只带工具注册状态的裸 agent（不跑 __init__、不连 LLM、不碰磁盘）。"""
    a = A.AIAgent.__new__(A.AIAgent)
    a._base_tools = [{"type": "function",
                      "function": {"name": "skill_help", "description": "skill_help",
                                   "parameters": {"type": "object", "properties": {}}}}]
    a._all_tools = list(a._base_tools)
    a._local_tool_names = {"skill_help"}
    a._activated_skills = set()
    a._skill_last_used = {}
    a._skill_order = []
    a._max_active_tools = lambda: max_tools
    _H.get_harness = lambda: _FakeHarness(_TABLE)  # agent 内部是 from harness import get_harness
    return a


# 上限必须远高于「实际会用到的技能总量」（全部技能合计 154 个工具）——
# 48 就是被这一条坑掉的：1 + 47 正好卡死，第二个技能一激活就截断。
check("★ 默认上限不再是 48（那是 1+47，卡死在 code_ops 上）",
      A.MAX_ACTIVE_TOOLS >= 100, "MAX_ACTIVE_TOOLS=%d" % A.MAX_ACTIVE_TOOLS)
_cfg8 = json.load(open(os.path.join(os.path.dirname(A.__file__), "settings.json"),
                       encoding="utf-8"))
_m8 = int((_cfg8.get("agent") or {}).get("max_active_tools") or 0)
check("★ settings.json 的 max_active_tools 同步放宽（否则配置盖掉默认值）",
      _m8 >= 100, "max_active_tools=%s" % _m8)

# 留痕：三个改动点各自报一声，否则只看得见「工具集变了」、看不见「谁改的」
_seen8 = []
_orig_trace = A._trace_tools_change
A._trace_tools_change = lambda reason, names, chars=0: _seen8.append(reason)

# 旧上限现场复现：第二个技能一激活，47 个工具整段被砍（这就是 48↔1）
_a48 = _mk_agent(max_tools=48)
_a48._activate_skill("code_ops")
_n_code = len(_a48._all_tools)
_a48._activate_skill("media")
check("★★ 旧上限 48 下：再激活一个技能就截断（48↔1 的现场）",
      len(_a48._all_tools) < _n_code,
      "%d → %d" % (_n_code, len(_a48._all_tools)))

# 新上限：连续激活，工具集只增不减，且旧工具仍在原位（追加而非重排）
_a9 = _mk_agent(max_tools=A.MAX_ACTIVE_TOOLS)
_a9._activate_skill("code_ops")
_a9._activate_skill("media")
_a9._activate_skill("tasks")
_n9 = A._tool_names(_a9._all_tools)
check("★★ 新上限下连续激活：只增不减（前缀不再被砍）",
      len(_n9) == 1 + 47 + 34 + 23, "%d 个工具" % len(_n9))
check("★ 新增工具追加在末尾，旧工具逐字保持原位",
      _n9[:3] == ["skill_help", "code_0", "code_1"]
      and _n9[-1] == "task_22" and _n9[47] == "code_46")

# 顺序稳定：重挂顺序由「激活先后」决定，不能交给 set 的迭代顺序
_b9 = _mk_agent(max_tools=A.MAX_ACTIVE_TOOLS)
_b9._activate_skill("media")
_b9._activate_skill("code_ops")
check("★ 重挂顺序 = 激活先后（与 set 迭代顺序无关）",
      A.AIAgent._ordered_active_skills(_a9) == ["code_ops", "media", "tasks"]
      and A.AIAgent._ordered_active_skills(_b9) == ["media", "code_ops"],
      "%s / %s" % (A.AIAgent._ordered_active_skills(_a9),
                   A.AIAgent._ordered_active_skills(_b9)))
_r8_1 = A._tool_names(_a9._all_tools)
_a9._stabilize_tools_for_new_turn()
_r8_2 = A._tool_names(_a9._all_tools)
check("★★ 连续两轮重挂结果逐字节一致（顺序不抖 = 前缀不废）",
      _r8_1 == _r8_2, "%d / %d" % (len(_r8_1), len(_r8_2)))

# 淘汰必须同步维护「激活先后」，否则顺序依据失真、下一轮重挂顺序就变
_a9._deactivate_skill("media")
check("★ 淘汰后 _skill_order 同步移除（否则顺序依据失真）",
      "media" not in _a9._skill_order and "media" not in _a9._activated_skills)

_a9.reset_session_skills()
A._trace_tools_change = _orig_trace
check("★★ 三个改动点都留痕（activate / evict / reset）",
      any(r.startswith("activate:") for r in _seen8)
      and any(r.startswith("evict:") for r in _seen8) and "reset" in _seen8,
      "%s" % sorted(set(_seen8))[:6])

# 死代码守卫：重挂接线必须走有序列表，且不能退回 list(set) 的写法
check("★★ 重挂走 _ordered_active_skills（不是 list(set)）",
      "for sname in self._ordered_active_skills():" in _src_a
      and "for sname in list(self._activated_skills):" not in _src_a)
check("★★ 工具集变化落盘路径已接线（data/tools_trace.jsonl）",
      "data\" / \"tools_trace.jsonl" in _src_a)
check("诊断落盘失败静默（绝不因它打断主流程）",
      A._trace_tools_change("selftest", ["x"]) is None)

# 实例标识：真实样本里同一 sid 反复 reset_sid，只有它能分辨
# 「视图被清」与「换了实例」——没它就只能猜。
check("★ hist_view 诊断带实例标识 inst/pid（分辨视图被清 vs 换了实例）",
      "\"inst\": inst" in _src_a and "\"pid\": os.getpid()" in _src_a)
check("★ 实例出生即留痕（instance_new）",
      'self._inst_tag = f"{os.getpid()}' in _src_a
      and '"instance_new"' in _src_a)
check("★ 观测字段容错：无 __init__ 的实例也不报错（trace 不碰主路径）",
      "getattr(self, \"_inst_tag\", \"\")" in _src_a)
check("★ trace_report 能报实例分布与前缀断裂",
      "实例数" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "trace_report.py"), encoding="utf-8").read())



# 重置归因：只记当前 sid 时「sid 变没变」根本看不见——两行 reset_sid 的 sid
# 都是同一个值（那是当前值，不是判定依据）。补上上一轮的 sid，reset 才能自证原因。
check("★ hist_view 诊断带上一轮 sid（psid）",
      '"psid": str(prev_sid or "")' in _src_a)
check("★ psid 取自 _hist_msgs_sid 并参与 reset 判定（不另存一份状态）",
      "prev_sid = getattr(self, \"_hist_msgs_sid\", None)" in _src_a
      and "reset = not isinstance(view, list) or prev_sid != sid" in _src_a)
check("★ 两处 trace 调用都带 prev_sid（漏一处就有一半样本无法归因）",
      _src_a.count("inst, prev_sid)") >= 2, str(_src_a.count("inst, prev_sid)")))
_src_r = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "trace_report.py"), encoding="utf-8").read()
check("★ trace_report 有重置归因（新实例 / 换会话 / 清视图 三分类）",
      "重置归因" in _src_r and "同会话清视图(真bug)" in _src_r)
# 归因分完类还要给终审，否则人得自己数「真bug」那一格——
# 而新实例首轮 reset 被算进「前缀断裂」会造假警报（实测 4 条里 1 条是这种）。
check("★ trace_report 给滞回终审结论（不用人肉数真bug）",
      "滞回判定" in _src_r)
check("★ 新实例首轮 reset 不计入前缀断裂（psid 空 = 无前缀可断）",
      "_real_break" in _src_r and 'm == "reset_sid" and "psid" in r' in _src_r)
# 「假接回」必须能被分辨：视图读回来了但锚点丢了，走的仍是全量重建。
# 报成 restore 就是假成功——白烧一分不少，报告却显示已修好。
check("★ 锚点丢失的接回报 restore_lost（不许假成功）",
      'mode = "restore_lost"' in _src_a and "elif anchor < 0:" in _src_a)
check("★ trace_report 给跨进程接回证据行（restore 到底发生没有）",
      "跨进程接回" in _src_r)
check("★ restore_lost 计入前缀断裂（它白烧一分不少）",
      '"trim", "restore_lost"' in _src_r)

# ============================================================
#  九、技能激活集跨进程持久化：重启不该等于「忘了自己会用哪个技能」
#
#  实测（data/turn_metrics.jsonl，12 轮）：工具集在 48 ↔ 1 之间跳了 5 次，
#  每次跳变都对应一次进程重启——`_activated_skills` 是纯内存 set，一重启就空。
#  tools 排在请求最前，它一动整条前缀全废：跳变那几轮断点回到 history[0]，
#  单轮白烧最高 78k 字符；顺带助手还会「忘了自己会用这个技能」，得重新
#  skill_help 一次。与历史视图落盘同一个病根：把进程生命周期当会话边界。
#  下面每一项都盯着一个「会让它悄悄退回原状」的具体写法。
# ============================================================
print("\n[9] 技能激活集跨进程持久化（_load_skills_state / _save_skills_state）")


class _FakeMemory:
    def __init__(self, sid):
        self.session_id = sid


_tmpdir9 = tempfile.mkdtemp(prefix="skills_state_")
_real_path9 = A.SKILLS_STATE_PATH
_real_enabled9 = A._skills_state_persist_enabled
A.SKILLS_STATE_PATH = os.path.join(_tmpdir9, "skills_state.json")
A._skills_state_persist_enabled = lambda: True      # 临时放开 tag=test 的闸门


def _mk_agent_sid(sid, max_tools=A.MAX_ACTIVE_TOOLS):
    a = _mk_agent(max_tools=max_tools)
    a.memory = _FakeMemory(sid)
    a._skills_restored_sid = None
    return a


# 真·重启场景：旧实例激活后落盘，新实例（激活集为空）同 sid 起来
_old9 = _mk_agent_sid("s1")
_old9._activate_skill("code_ops")
_old_names9 = A._tool_names(_old9._all_tools)
check("激活后即落盘（否则重启无档可读）",
      A._load_skills_state("s1") == ["code_ops"], "%s" % A._load_skills_state("s1"))

_new9 = _mk_agent_sid("s1")
check("新实例出生时激活集为空（复现重启现场）", not _new9._activated_skills)
_new9._stabilize_tools_for_new_turn()
check("★★ 重启后同 sid 自动重挂，工具集与上一进程逐字节一致",
      A._tool_names(_new9._all_tools) == _old_names9 and len(_old_names9) == 48,
      "%d 个 / 期望 %d 个" % (len(_new9._all_tools), len(_old_names9)))

_old9b = _mk_agent_sid("s2")
_old9b._activate_skill("code_ops")
_old9b._activate_skill("media")
_old_names9b = A._tool_names(_old9b._all_tools)
_new9b = _mk_agent_sid("s2")
_new9b._stabilize_tools_for_new_turn()
check("★★ 多技能重启后顺序也一致（顺序抖 = 前缀照样废）",
      A._tool_names(_new9b._all_tools) == _old_names9b,
      "%d / %d" % (len(_new9b._all_tools), len(_old_names9b)))

check("★ 换 sid 不接旧技能（防跨会话串技能）",
      A._load_skills_state("s9") == [] and not _mk_agent_sid("s9")._restore_skills())
check("空 sid 直接当没有状态",
      A._load_skills_state("") == [] and not _mk_agent_sid("")._restore_skills())

# sid 未就绪不能置「已评估」标志，否则这一进程再也不试恢复
_na9 = _mk_agent_sid("")
_na9._restore_skills()
check("★ sid 未就绪不置已评估标志（下一轮还能恢复）",
      _na9._skills_restored_sid is None, "%r" % (_na9._skills_restored_sid,))

# 恢复只评估一次：同 sid 重复调用不该反复重挂
# （上一步 s2 的落盘覆盖了文件，这里把 s1 补回来——不补就会把「读不到」当成「没恢复」）
A._save_skills_state("s1", ["code_ops"])
_b9 = _mk_agent_sid("s1")
_first9 = _b9._restore_skills()
_second9 = _b9._restore_skills()
check("★ 同一 sid 只恢复一次（重复调用不重复评估）",
      _first9 == {"code_ops"} and _second9 == set(),
      "%s / %s" % (sorted(_first9), sorted(_second9)))

# 恢复路径与用户主动激活必须能分辨：否则看日志分不清工具集是自己长回来的
# 还是人又激活了一次——而这两件事的处置完全不同（后者要查为何会重启）。
_seen9 = []
A._save_skills_state("s1", ["code_ops"])
_orig_trace9 = A._trace_tools_change
A._trace_tools_change = lambda reason, names, chars=0: _seen9.append(reason)
_c9 = _mk_agent_sid("s1")
_c9._stabilize_tools_for_new_turn()
A._trace_tools_change = _orig_trace9
check("★ 恢复路径打 restore: 前缀（与用户主动 activate: 区分）",
      any(r.startswith("restore:") for r in _seen9)
      and not any(r.startswith("activate:") for r in _seen9), "%s" % _seen9)

# 新会话必须清盘：盘上的激活集串到新会话 = 跨会话带上下文
_d9 = _mk_agent_sid("s1")
_d9._activate_skill("code_ops")
_d9.reset_session_skills()
check("★ 新会话清盘（盘上的激活集不串到新会话）",
      A._load_skills_state("s1") == [] and not _d9._activated_skills)

# 坏文件/坏类型一律降级为「没有状态」，绝不抛异常
with open(A.SKILLS_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write("{坏 JSON")
check("★ 状态文件损坏 → 降级为「没有技能」，绝不抛异常",
      A._load_skills_state("s1") == [])

with open(A.SKILLS_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write(json.dumps({"sid": "s1", "skills": "不是列表"}))
check("skills 类型不对 → 当作没有状态（不猜）", A._load_skills_state("s1") == [])

with open(A.SKILLS_STATE_PATH, "w", encoding="utf-8") as _f:
    _f.write(json.dumps({"sid": "s1", "skills": [1, "code_ops", None, ""]}))
check("非字符串元素被过滤，只剩合法技能名",
      A._load_skills_state("s1") == ["code_ops"])

A.SKILLS_STATE_PATH = _real_path9
A._skills_state_persist_enabled = _real_enabled9
check("★ 自检(tag=test)不读写真实技能状态文件",
      A._skills_state_persist_enabled() is False)

# 死代码守卫：「定义存在」不等于「生效」——恢复必须接在每轮稳定工具集处
check("★★ 技能恢复已接进 _stabilize_tools_for_new_turn（不是只定义不调用）",
      "restored = self._restore_skills()" in _src_a
      and "self._activate_skill(sname, restored=sname in restored)" in _src_a)
check("★★ 激活/淘汰/新会话三处都维护盘上状态（漏一处就有一半场景退化）",
      _src_a.count("_save_skills_state(") >= 3 and "_clear_skills_state()" in _src_a,
      "save×%d" % _src_a.count("_save_skills_state("))
check("★ 技能状态落盘路径已接线（data/skills_state.json）",
      'data", "skills_state.json' in _src_a)
check("★ 技能状态读写都容错（无 memory 的裸实例也不报错）",
      'getattr(getattr(self, "memory", None), "session_id", None)' in _src_a)


# ---- [10] 切角色卡片保留技能（_carry_skills_to）----
# 技能是 Agent 的能力，人设是说话风格，两者正交：切卡片换的是记忆空间，
# 不该让 Agent 忘掉自己会用哪些工具。实测 data/turn_metrics.jsonl：
# tools 掉到 1 的轮次占 16.4%（34/207），吃掉全部 miss 的 20.7%。
# 这一段要读写落盘状态：临时把路径指到临时目录 + 放开 tag=test 闸门，
# 结束时必须还原——否则会覆盖真实的 data/skills_state.json。
_tmpdir10 = tempfile.mkdtemp(prefix="carry_skills_")
A.SKILLS_STATE_PATH = os.path.join(_tmpdir10, "skills_state.json")
A._skills_state_persist_enabled = lambda: True

_c10 = _mk_agent_sid("card_a")
_c10._activate_skill("code_ops")
_names_before10 = A._tool_names(_c10._all_tools)
check("切卡片前技能已挂上（前提成立才谈得上保留）",
      len(_names_before10) > 20, "%d 个" % len(_names_before10))

_c10._carry_skills_to("card_b")          # 模拟切角色卡片：换记忆空间
check("★★ 切卡片后工具集逐字节不变（能力不随人设重置）",
      A._tool_names(_c10._all_tools) == _names_before10,
      "%d / %d" % (len(A._tool_names(_c10._all_tools)), len(_names_before10)))
check("★★ 激活集改挂到新 sid（不改挂 = 下一轮 restore 读不到，白保留）",
      A._load_skills_state("card_b") == _c10._ordered_active_skills(),
      "%s" % A._load_skills_state("card_b"))
check("★ 新 sid 已认领，不让 restore 再覆盖一次",
      _c10._skills_restored_sid == "card_b")
check("★ 空 sid 不写档（不猜，宁可下次再说）",
      (_c10._carry_skills_to(None) or True)
      and A._load_skills_state("card_b") == ["code_ops"])

# 真·新会话仍必须清盘（用户点「新会话」按钮）——该清的要清，别一起放行
_c10.reset_session_skills()
check("★ 用户点新会话仍清盘（保留 ≠ 永不清理）",
      A._load_skills_state("card_b") == [] and not _c10._activated_skills)

# 接线守卫：两个角色卡片入口都必须走 _carry_skills_to，不许退回 reset
check("★★ 切卡片两个入口都走 _carry_skills_to（退回 reset 就前功尽弃）",
      _src_a.count("self._carry_skills_to(self.memory.session_id)") >= 2,
      "carry×%d" % _src_a.count("self._carry_skills_to(self.memory.session_id)"))

A.SKILLS_STATE_PATH = _real_path9
A._skills_state_persist_enabled = _real_enabled9

# ---- [11] 「太重」按字符量判，不按工具个数 ----
check("★★ 字符上限独立于个数上限（胖工具按字符拦）",
      hasattr(A, "MAX_ACTIVE_TOOLS_CHARS") and A.MAX_ACTIVE_TOOLS_CHARS >= 40000,
      "chars=%s" % getattr(A, "MAX_ACTIVE_TOOLS_CHARS", None))
check("★ 字符上限有下限保护（配置写 0 不能把所有工具砍光）",
      _mk_agent_sid("s1")._max_active_tools_chars() >= 8000,
      "%d" % _mk_agent_sid("s1")._max_active_tools_chars())
check("★ 字符上限可被 settings.json 覆盖（调参不必改代码）",
      "max_active_tools_chars" in _src_a)

# ---- [12] 自检不许污染真实诊断文件 ----
# tools_trace.jsonl 是「真实环境里谁在改工具集」的唯一依据，而自检每跑一次
# 就会触发一串 reset/activate。实测踩过：40 条 reset 里只有 4 条是真的。
_real_base10 = A.BASE_DIR
A.BASE_DIR = __import__("pathlib").Path(_tmpdir9)
A._skills_state_persist_enabled = lambda: False
A._trace_tools_change("reset", ["x"], 1)
_f10 = os.path.join(_tmpdir9, "data", "tools_trace.jsonl")
check("★ 自检(tag=test)不写 tools_trace（否则真实归因被回放污染）",
      not os.path.exists(_f10))
A._skills_state_persist_enabled = lambda: True
A._trace_tools_change("reset", ["x"], 1)
check("★ 守卫放开后正常落盘（守卫不能误杀真实记录）", os.path.exists(_f10))
A.BASE_DIR = _real_base10
A._skills_state_persist_enabled = _real_enabled9

# 续跑轮（resume）也不能跳过工具集重建：断点里不存 tools，跳过重建就等于
# 让模型用「重启后的空技能集」——技能工具调不出，tools 段还与断点不一致。
_chat_head9 = _src_a.split("async def chat_stream", 1)[-1].split("if resume is None:", 1)[0]
check("★★ 续跑轮也重建工具集（stabilize 不在 if resume 块内）",
      "self._stabilize_tools_for_new_turn()" in _chat_head9)


print("\n=== 结果：%d 项通过，%d 项失败 ===" % (len(ok), len(bad)))
if bad:
    for x in bad:
        print("  ✗ " + x)
    sys.exit(1)
