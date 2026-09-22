"""验证记忆压缩三处改动（2026-09-22）：摘要去重 / 用户原话保底 / 工具结果留头留尾。

跑法：venv/bin/python tools/mem_compress_verify.py
每项都打印「实测值 vs 期望」，失败即非零退出。
"""
import sqlite3
import sys

sys.path.insert(0, "/home/wxf/dabai")
import memory as M  # noqa: E402

fails = []


def check(name, got, want, ok=None):
    good = ok if ok is not None else (got == want)
    print(f"{'✅' if good else '❌'} {name}: 实测={got!r} 期望={want!r}")
    if not good:
        fails.append(name)


# ---- 1. 留头留尾 ----
t = "HEAD" * 400 + "MIDDLE" * 400 + "TAIL" * 400
r = M._truncate_head_tail(t, 500)
check("留头留尾长度恰好=上限", len(r), 500)
check("头部保留", r.startswith("HEAD"), True)
check("尾部保留（结论在尾）", r.endswith("TAIL"), True)
check("中间有省略标记", "中间省略" in r, True)
short = "x" * 100
check("不超上限时原样返回", M._truncate_head_tail(short, 500) is short, True)

# ---- 2. 摘要去重（真实库数据） ----
conn = sqlite3.connect("/home/wxf/dabai/chat_memory.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT summary_text FROM summaries WHERE session_id=? "
    "ORDER BY created_at DESC LIMIT ?", ("ab4d39164c7c", M.MAX_SUMMARIES)).fetchall()
raw = [x["summary_text"] for x in rows]
dedup = M._dedupe_summaries(raw)
check(f"库里取回 {len(raw)} 条摘要 → 去重后", len(dedup), 1)
check("去重后不再重复同一句", "；".join(dedup).count("自动更新器"), 1)
check("写入口判重：同一句视为冗余",
      M._summary_is_redundant("用户问了A。经查B。", "用户问了A。经查B。"), True)
check("写入口判重：新信息不算冗余",
      M._summary_is_redundant("用户问了A。", "用户问了A。经查B存在。"), False)
conn.close()

# ---- 3. 用户原话保底 + 工具结果留头留尾（走真实打包函数） ----
user_long = "我的要求是" + "详" * 2000 + "结尾必须保留"
recs = []
for i in range(3):
    recs.append({"role": "user", "content": user_long if i == 2 else f"第{i}轮问题"})
    recs.append({"role": "assistant", "content": "",
                 "tool_calls": [{"id": f"c{i}", "function": {"name": "shell_run",
                                                             "arguments": "{}"}}]})
    recs.append({"role": "tool", "tool_call_id": f"c{i}",
                 "content": "输出开始" + "中" * 3000 + "exit=0"})
recs.append({"role": "user", "content": "最新一轮"})

packed = M._pack_history_records(
    recs, 8000, M.SHORT_TERM_MAX_CHARS_PER_ROUND,
    min_rounds=M.SHORT_TERM_MIN_ROUNDS,
    max_chars_per_tool=M.SHORT_TERM_MAX_CHARS_PER_TOOL,
    max_chars_per_tool_call=M.SHORT_TERM_MAX_CHARS_PER_TOOL_CALL,
    keep_last_tools=M.SHORT_TERM_KEEP_LAST_TOOLS,
    max_chars_per_tool_newest=M.SHORT_TERM_MAX_CHARS_PER_TOOL_NEWEST,
    keep_last_tools_newest=M.SHORT_TERM_KEEP_LAST_TOOLS_NEWEST,
    user_guaranteed_cap=M.SHORT_TERM_USER_GUARANTEED_CAP)

longs = [m for m in packed if m["role"] == "user" and len(m["content"]) > 1200]
check("保底轮里的长用户消息突破了 1200 上限", len(longs), 1)
if longs:
    check("用户原话头尾都在（不只是头）",
          longs[0]["content"].startswith("我的要求是")
          and longs[0]["content"].endswith("结尾必须保留"), True)
tools = [m for m in packed if m["role"] == "tool"]
check("工具结果条数", len(tools) > 0, True)
if tools:
    check("工具结果尾部结论（exit=0）保留", tools[0]["content"].endswith("exit=0"), True)
    check("工具结果受单条上限约束",
          all(len(m["content"]) <= M.SHORT_TERM_MAX_CHARS_PER_TOOL for m in tools), True)

print()
if fails:
    print(f"❌ {len(fails)} 项未通过：{fails}")
    sys.exit(1)
print("✅ 全部通过")
