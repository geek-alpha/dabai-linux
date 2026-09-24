#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条经验写进 harness 经验库（harness_task_memory.json）。

背景（2026-09-12）：harness/tasks.py:732 的 _remember_lesson 只在 flow/batch 任务
终态被调用，对话层的错误无从沉淀——rg 全项目除 tasks.py 自身零调用，
harness_task_memory.json 全盘不存在，这台学习机从未通电。本脚本给它一个对话层入口。

用法：python tools/lesson_add.py "教训文本"（--force 跳过写入闸门）

写入闸门（2026-09-20）：负面清单四类硬拒 + 两类软提示。依据
hermes-agent/agent/background_review.py:392-415 的 _DO_NOT_CAPTURE_BLOCK——
那四类写下去不会变成教训，会变成下次的自我设限。
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "harness_task_memory.json"
ARCHIVE = BASE / "harness_task_memory.archive.json"
# 上限只防无限膨胀，不是价值判据：注入窗口由 agent.py:_gene_pick 的选择压力控制
# （新近位 + 最少曝光优先），库容量不影响 prompt 质量，所以不拿新近度当淘汰标准。
MAX_LESSONS = 500


def _key(text):
    """与 tools/gene_fitness.py:key_of 同构，曝光流水的键靠它对齐。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


# ---- 写入闸门 ----
# 硬拒的判据必须能说清「为什么这句不能写」，否则调用方只会觉得被闸门随机拦了。
_FIX = re.compile(
    r"安装|配置|设置|改用|换成|替代|修|补上|升级|重启|设好|install|pip |apt|npm |brew|export |chmod|sudo")
_ENV_FAIL = re.compile(
    r"未安装|没装|缺少依赖|依赖缺失|No module named|ModuleNotFoundError|command not found|"
    r"未配置|没配置|凭证缺失|环境变量未|不是内部或外部命令|Permission denied")
_NEG_TOOL = re.compile(
    r"(工具|接口|API|插件|脚本|命令|服务)[^。；\n]{0,10}(坏了|废了|失效|不可用|不能用|用不了)")
_UNRESOLVED = re.compile(r"未解决|没能解决|都没成功|全部失败|仍未|待人工|需手动|无解")
_PRESCRIBE = re.compile(r"建议|推荐|应该|最佳实践|标准做法|照这个|流程就是")
_TRANSIENT = re.compile(r"重试(就|后)?(成功|好了|正常|通过)|再试一次(就|才)?(成功|好了)")
_TRANSIENT_RULE = re.compile(r"模式|规则|先重试|重试优先|一律重试")
# 故障只当引子、后面给出明确做法的不算负面断言：真库 364 条里两条假阳性都是这种
# 形态（「命令报…不是内部或外部命令→要把脚本写成 .ps1」），不加这条豁免会误伤规则本身。
_RULE_WORD = re.compile(r"要|不许|必须|应该|改成|返回|换成|改用|优先|只用")
_WHY = re.compile(r"因为|原因是|机制|否则|不这样|会导致|后果|之所以|——")
_ANCHOR = re.compile(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|#\d{2,}")


def _screen(text):
    """返回 (硬拒, 软提示)，元素是 (原因, 怎么改)。

    软提示不拦写入：判据宽了会误伤大量「现象+规则」的正常写法，而误伤会让人
    直接 --force 绕过去，闸门就废了。只硬拦那四类会被未来的自己引用来拒绝干活的。
    """
    hard, soft = [], []
    rule = bool(_RULE_WORD.search(text))
    if _ENV_FAIL.search(text) and not _FIX.search(text) and not rule:
        hard.append(("环境依赖失败（缺包/缺命令/缺凭证）不是可复用的规则",
                     "只写 FIX：装什么、配哪个变量、跑哪条命令"))
    if _NEG_TOOL.search(text) and not rule:
        hard.append(("对工具的负面断言会硬化成未来的自我拒绝",
                     "改成「什么条件下换哪条路」；当时的故障若已修好就别写"))
    if _UNRESOLVED.search(text) and _PRESCRIBE.search(text):
        hard.append(("未解决的失败不许写成推荐流程",
                     "要么不写，要么只写你独立确信可行的那条替代路径"))
    if _TRANSIENT.search(text) and not _TRANSIENT_RULE.search(text):
        hard.append(("会话内自愈的瞬时故障不是教训",
                     "写重试模式（重试几次、什么条件下退避），不写那次故障本身"))
    if not _WHY.search(text):
        soft.append("缺 WHY：补一句机制，只写结论下次会照错做")
    if _ANCHOR.search(text):
        soft.append("含日期/编号锚点：规则要脱离那次事件独立成立")
    return hard, soft


def _archive(texts, ts):
    """溢出条目归档而非蒸发：淘汰必须可见、可捞回。"""
    if not texts:
        return
    try:
        old = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    keep = old.get("lessons") if isinstance(old, dict) else None
    keep = [str(x) for x in keep] if isinstance(keep, list) else []
    keep.extend(texts)
    keep_ts = old.get("ts") if isinstance(old, dict) else None
    keep_ts = dict(keep_ts) if isinstance(keep_ts, dict) else {}
    keep_ts.update(ts)
    tmp = str(ARCHIVE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lessons": keep, "ts": keep_ts}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ARCHIVE)


def _read_cmd(argv) -> int:
    """按需读取入口：注入段只放摘要，全文从这里取。

    背景（2026-09-22）：注入段每轮常驻，把 77 条经验全塞进去就是拿上下文换安全感——
    摘要 + 按需读全文多一次工具调用，但常态只留 3 条。
    """
    try:
        data = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception as e:
        print(f"经验库读取失败：{e.__class__.__name__}: {e}")
        return 1
    ls = data.get("lessons") if isinstance(data, dict) else None
    ls = [str(x) for x in ls] if isinstance(ls, list) else []
    if not ls:
        print("经验库为空")
        return 0
    if argv[0] == "list":
        limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20
        print(f"共 {len(ls)} 条（最新在前）：")
        for i, t in enumerate(ls[:limit], 1):
            print(f"{i:>3}. [{_key(t)}] {t[:80]}")
        if len(ls) > limit:
            print(f"…（还有 {len(ls) - limit} 条）")
        return 0
    key = argv[1] if len(argv) > 1 else ""
    if not key:
        print("用法：python tools/lesson_add.py show <序号|hash前缀>")
        return 2
    if key.isdigit() and 1 <= int(key) <= len(ls):
        t = ls[int(key) - 1]
    else:
        hit = [t for t in ls if _key(t).startswith(key)]
        if len(hit) != 1:
            print(f"匹配 {len(hit)} 条，换个更长的 hash 前缀" if hit else "没有匹配")
            return 3
        t = hit[0]
    print(f"[{_key(t)}]\n{t}")
    return 0


USAGE = ('用法：python tools/lesson_add.py "教训文本"（--force 跳过写入闸门）'
         '\n       python tools/lesson_add.py list [N] ｜ show <序号|hash前缀>')


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--force"]
    force = len(argv) != len(sys.argv[1:])
    # 试探性调用绝不能写库：本脚本原先不认 --help，那一串参数被当成教训文本
    # 写成了第 316 条（内容是字面的 "--help"），只能人肉清库。
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv and argv[0] in ("list", "show"):
        return _read_cmd(argv)
    text = " ".join(argv).strip()
    if not text:
        print(USAGE)
        return 2
    hard, soft = _screen(text)
    if hard and not force:
        print("拒绝写入 —— 这类内容下次会变成自我设限：")
        for why, how in hard:
            print(f"  ✗ {why}\n    → {how}")
        print("确实要写就加 --force。")
        return 3
    try:
        data = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception:
        data = {}
    ls = data.get("lessons") if isinstance(data, dict) else None
    ls = [str(x) for x in ls] if isinstance(ls, list) else []
    ts = data.get("ts") if isinstance(data, dict) else None
    ts = {str(k): v for k, v in ts.items()} if isinstance(ts, dict) else {}
    if text in ls:
        print(f"已存在（第 {ls.index(text) + 1} 条），未重复写入")
        return 0
    ls.insert(0, text)
    ts[_key(text)] = round(time.time(), 1)
    dropped = ls[MAX_LESSONS:]
    ls = ls[:MAX_LESSONS]
    live = {_key(t) for t in ls}
    # 库和 ts 表必须同生共死：残留键会被下一个同文条目继承成假时间
    _archive(dropped, {k: v for k, v in ts.items() if k not in live})
    ts = {k: v for k, v in ts.items() if k in live}
    tmp = str(FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lessons": ls, "ts": ts}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)
    tail = f"；已满 {MAX_LESSONS} 条，最老的 {len(dropped)} 条归档到 {ARCHIVE.name}" if dropped else ""
    print(f"已记录（共 {len(ls)} 条）：{text[:80]}{tail}")
    for s in soft:
        print(f"  建议：{s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
