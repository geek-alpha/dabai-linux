"""验证摘要原文索引（2026-09-22）：落盘 / 累计去重 / 判重不丢指针 / 组装不重复正文。

跑法：venv/bin/python tools/spill_index_verify.py
只读真实库结构，写的是临时库与 spill 目录，跑完清掉自己造的文件。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory
from memory import (ChatMemory, _dedupe_summaries, _merge_spill_index,
                    _spill_index_for, _split_spill_index, _SPILL_INDEX_HEAD,
                    _SPILL_INDEX_MAX_ITEMS)

FAILS = []


def check(name, got, exp):
    ok = got == exp
    print(("✅ " if ok else "❌ ") + f"{name}: 实测={got!r} 期望={exp!r}")
    if not ok:
        FAILS.append(name)


LONG1 = "第一段输出内容\n" * 200      # 1400 字，超 _SPILL_INDEX_MIN_CHARS
LONG2 = "第二段输出内容\n" * 200
# 用户原话要超 _SUMMARY_DEDUPE_MIN_LEN(20) 字，否则短摘要按设计不参与去重
USER_TEXT = "看下这个结果，然后告诉我你发现了什么问题，别重跑命令"

# ---- 1. 落盘 + 索引生成 ----
idx = _spill_index_for([
    {"role": "tool", "content": LONG1},
    {"role": "tool", "content": "短结果"},
    {"role": "assistant", "content": LONG1},
])
check("索引头正确", idx.startswith(_SPILL_INDEX_HEAD), True)
check("只收超长工具结果（1 条）", len(idx[len(_SPILL_INDEX_HEAD):].split("；")), 1)
name1 = idx[len(_SPILL_INDEX_HEAD):].split("(", 1)[0]
check("落盘文件存在", (memory._TOOL_SPILL_DIR / name1).exists(), True)
check("短消息不产生索引", _spill_index_for([{"role": "tool", "content": "ok"}]), "")

# ---- 2. 累计去重 + 封顶 ----
a = _SPILL_INDEX_HEAD + "aaa.txt(100字)；bbb.txt(200字)"
b = _SPILL_INDEX_HEAD + "bbb.txt(200字)；ccc.txt(300字)"
m = _merge_spill_index(a, b)
check("旧条目保留", "aaa.txt" in m, True)
check("同名去重", m.count("bbb.txt"), 1)
check("顺序旧→新", m.index("aaa.txt") < m.index("ccc.txt"), True)
many = _SPILL_INDEX_HEAD + "；".join(f"f{i}.txt({i}字)" for i in range(10))
check("条数封顶", len(_merge_spill_index(many, "").split("；")), _SPILL_INDEX_MAX_ITEMS)

# ---- 3. 拆分往返 ----
body = "这是一段摘要正文。"
text = f"{_SPILL_INDEX_HEAD}aaa.txt(100字)\n{body}"
b1, i1 = _split_spill_index(text)
check("拆出正文", b1, body)
check("拆出索引", i1, f"{_SPILL_INDEX_HEAD}aaa.txt(100字)")
check("无索引时原样返回", _split_spill_index(body), (body, ""))


# ---- 4. 落盘索引：记录、不重复记、剪枝同步清 ----
def test_index():
    real_dir, real_idx = memory._TOOL_SPILL_DIR, memory._TOOL_SPILL_INDEX
    real_max = memory._TOOL_SPILL_MAX_FILES
    tmp_dir = Path(tempfile.mkdtemp()) / "spill"
    # 把目录和上限都指向临时区：剪枝测试真的会删文件，不能在真实 spill 目录里跑
    memory._TOOL_SPILL_DIR = tmp_dir
    memory._TOOL_SPILL_INDEX = tmp_dir / "index.jsonl"
    memory._TOOL_SPILL_MAX_FILES = 2

    def entries():
        if not memory._TOOL_SPILL_INDEX.exists():
            return []
        out = []
        for line in memory._TOOL_SPILL_INDEX.read_text(encoding='utf-8').splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out

    try:
        memory._spill_tool_text(LONG1, "shell_run")
        memory._spill_tool_text(LONG2, "code_search")
        es = entries()
        check("索引记了两条", len(es), 2)
        check("索引带工具名", {e.get('tool') for e in es}, {"shell_run", "code_search"})
        check("索引带预览", all(e.get('preview') for e in es), True)
        check("索引字数等于正文", {e.get('chars') for e in es}, {len(LONG1), len(LONG2)})
        check("索引指向的正文都在", all((tmp_dir / e['file']).exists() for e in es), True)

        memory._spill_tool_text(LONG1, "shell_run")
        check("同一文本重复落盘不重复记", len(entries()), 2)

        memory._spill_tool_text("第三段输出内容\n" * 200, "read_lines")
        files = sorted(p.name for p in tmp_dir.iterdir()
                       if p.is_file() and p.name != "index.jsonl" and not p.name.endswith('.tmp'))
        check("正文受文件数上限约束", len(files), 2)
        check("索引只留活着的文件", sorted(e['file'] for e in entries()), files)
        check("索引文件自己没被剪掉", memory._TOOL_SPILL_INDEX.exists(), True)
    finally:
        memory._TOOL_SPILL_DIR = real_dir
        memory._TOOL_SPILL_INDEX = real_idx
        memory._TOOL_SPILL_MAX_FILES = real_max


test_index()


# ---- 4b. 工具名解析：真实消息里名字挂在前一条 assistant 的 tool_calls 上 ----
_msgs = [
    {"role": "assistant", "tool_calls": [
        {"id": "call_1", "function": {"name": "shell_run", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "x"},
]
check("从 tool_calls 解析出工具名",
      memory._tool_name_of(_msgs[1], memory._tool_names_by_call_id(_msgs)), "shell_run")
check("无映射时留空不报错", memory._tool_name_of(_msgs[1]), "")
check("消息自带 name 优先",
      memory._tool_name_of({"name": "x", "tool_call_id": "call_1"}, {"call_1": "y"}), "x")


# ---- 4c. DB 形状的轮：tool 消息没有 tool_call_id（messages 表没这列）----
_db_rnd = [
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "shell_run", "arguments": "{}"}},
        {"id": "c2", "function": {"name": "read_lines", "arguments": "{}"}}]},
    {"role": "tool", "content": "a"},
    {"role": "tool", "content": "b"},
]
_nm = memory._tool_names_in_order(_db_rnd)
check("无 id 时按顺序对齐工具名", [_nm.get(id(m)) for m in _db_rnd[1:]], ["shell_run", "read_lines"])
check("非 tool 消息不入表", len(_nm), 2)


# ---- 5. 端到端：真写库 + 真组装 ----
async def main():
    tmp = Path(tempfile.mkdtemp())
    memory.DB_PATH = tmp / "verify.db"
    # 模块 import 时已在真实库上建了线程本地缓存连接，不清掉的话 _init_db 会把表
    # 建在真实 chat_memory.db，而工作线程又连到空的临时库（no such table: sessions）
    memory._get_db().close()
    memory._db_local.conn = None
    memory._init_db()
    before = {p.name for p in memory._TOOL_SPILL_DIR.iterdir() if p.is_file()}

    cm = ChatMemory("u_verify", None, "default")
    await cm.get_or_create_session("索引验证")
    # 两次摘要：正文相同（关键词摘要确定）、索引各新增一条 → 都必须入库
    await cm._generate_summary([{"id": 1, "role": "tool", "content": LONG1},
                                {"id": 2, "role": "user", "content": USER_TEXT}])
    await cm._generate_summary([{"id": 3, "role": "tool", "content": LONG2},
                                {"id": 4, "role": "user", "content": USER_TEXT}])

    conn = memory._get_db()
    rows = [r["summary_text"] for r in conn.execute(
        "SELECT summary_text FROM summaries WHERE session_id=? ORDER BY created_at DESC",
        (cm.session_id,)).fetchall()]
    conn.close()
    check("两次摘要都入库（索引新增不算冗余）", len(rows), 2)
    check("库里最新摘要带索引头", rows[0].startswith(_SPILL_INDEX_HEAD), True)
    check("库里存了指针原文", all(r.startswith(_SPILL_INDEX_HEAD) for r in rows), True)

    ctx = await cm.build_hierarchical_context(query="结果")
    block = ctx.get("summary_block") or ""
    check("组装块含索引", _SPILL_INDEX_HEAD in block, True)
    check("组装块索引含两个文件", block.count(".txt"), 2)
    check("组装块正文不重复", block.count(USER_TEXT), 1)

    added = {p.name for p in memory._TOOL_SPILL_DIR.iterdir() if p.is_file()} - before
    added.discard(memory._TOOL_SPILL_INDEX.name)  # 索引不是测试产物，删了它等于把真实索引清空
    memory._spill_index_drop(added)  # 正文删了索引也要删：留着就是死指针
    for n in added:
        (memory._TOOL_SPILL_DIR / n).unlink(missing_ok=True)
    print(f"（清理临时 spill 文件 {len(added)} 个）")
    print(f"（临时库 {tmp}）")


asyncio.run(main())

print()
print(f"❌ {len(FAILS)} 项未通过：{FAILS}" if FAILS else "✅ 全部通过")
sys.exit(1 if FAILS else 0)
