# -*- coding: utf-8 -*-
"""重放历史失败调用：验证 A/C 两类修复是否真能拦住当时那些错。

历史 trace 不会变，所以「修完重跑 err_recidivism.py 看下降」是无效判据——那读的是
同一份旧数据。真正可判的是：把当时那些 (tool, args) 拿新校验层原地重放一遍，看错误
还在不在。零 API 成本，不用等新 cycle。

判据（刻意分开报，不混成一个「修好了」）：
  A 类「尚未注册」——该错误与参数无关，属主能加载就该消失；参数被截断的记录也能判。
  参数完整的那部分另算「直接校验通过」，这才是「这一次本来能跑成」的证据。
  C 类「期望数组」——必须参数完整才可判，截断的不计入。
"""
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent  # noqa: E402
import harness  # noqa: E402
from tool_validation import find_tool_spec  # noqa: E402

# 真 _activate_skill 会写会话技能状态和 tools 流水——重放跑一次就往真实统计里塞假的
# 「激活」，后续观测（gene_fitness/tools_trace）全被污染。两个落盘副作用在这里掐掉。
agent._save_skills_state = lambda *a, **k: None
agent._trace_tools_change = lambda *a, **k: None

TRACES = os.path.join(BASE, "data", "longrun", "traces")
UNREGISTERED = "尚未注册"
ARRAY_MISMATCH = "期望数组"


class _Stub:
    """只提供 _validate_tool_call / _activate_skill 真正读写的字段。"""

    def __init__(self, tools):
        self._all_tools = list(tools)
        self._local_tool_names = set()
        self._skill_last_used = {}
        self._activated_skills = set()
        self._skill_order = []

    def _max_active_tools(self):
        return 999

    def _max_active_tools_chars(self):
        return 10 ** 9

    def _activate_skill(self, name, restored=False):
        return agent.AIAgent._activate_skill(self, name, restored)

    def _ordered_active_skills(self):
        return agent.AIAgent._ordered_active_skills(self)


def _cold_stub():
    """每次重放都从冷启动开始：历史上报错时正是「什么都没加载」。"""
    return _Stub(agent.load_local_tools())


def _unwrap(raw):
    """ToolCallStart.arguments 有两种记录形状：直接是 args，或包一层 {"arguments": {...}}。

    后者实测 11/1027 次（集中在 shell_run），是写入端两种格式并存，不是模型传错参数。
    按 key 集合精确识别——「有 arguments 键就剥」会误伤真带 arguments 参数的工具。
    """
    if not isinstance(raw, dict):
        return None
    if set(raw.keys()) == {"arguments"} and isinstance(raw.get("arguments"), dict):
        return raw["arguments"]
    return raw


def load_failures():
    """按顺序配对 ToolCallStart(arguments) → ToolCallResult，取失败项。

    args_ok=False 表示该次 arguments 被写入端截断（超长 content/old 字段），JSON 不完整，
    参数无法还原——A 类仍可判，C 类不可判。
    """
    rows = []
    shapes = {"flat": 0, "wrapped": 0, "truncated": 0}
    for path in sorted(glob.glob(os.path.join(TRACES, "*.jsonl"))):
        pending = []
        for line in open(path, encoding="utf-8", errors="ignore"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "ToolCallStart":
                raw, args_ok = None, True
                try:
                    raw = json.loads(ev.get("arguments") or "{}")
                except ValueError:
                    args_ok = False
                    shapes["truncated"] += 1
                if raw is not None and isinstance(raw, dict):
                    shapes["wrapped" if set(raw.keys()) == {"arguments"} else "flat"] += 1
                pending.append((ev.get("tool_name"), _unwrap(raw), args_ok))
            elif kind == "ToolCallResult":
                name, args, args_ok = pending.pop(0) if pending else (None, None, False)
                if str(ev.get("success")) in ("True", "true"):
                    continue
                rows.append({
                    "file": os.path.basename(path),
                    "tool": ev.get("tool_name") or name,
                    "args": args,
                    "args_ok": args_ok,
                    "err": str(ev.get("result", "")),
                })
    return rows, shapes


def replay(tool, args):
    """冷启动重放一次，返回 (通过?, 错误文本)。args 为 None 时按空参跑。"""
    stub = _cold_stub()
    _, err = agent.AIAgent._validate_tool_call(stub, tool, dict(args or {}))
    return err is None, err


def main():
    rows, shapes = load_failures()
    a_rows = [r for r in rows if UNREGISTERED in r["err"]]
    c_rows = [r for r in rows if ARRAY_MISMATCH in r["err"]]
    other = [r for r in rows if r not in a_rows and r not in c_rows]

    print("trace 参数形状：直接 %d / 包一层 %d / 截断不可还原 %d"
          % (shapes["flat"], shapes["wrapped"], shapes["truncated"]))
    print("失败调用 %d 次：A 技能未加载 %d / C 数组类型 %d / 其他 %d"
          % (len(rows), len(a_rows), len(c_rows), len(other)))

    for label, group, needle in (("A 技能未加载", a_rows, UNREGISTERED),
                                 ("C 数组类型", c_rows, ARRAY_MISMATCH)):
        if not group:
            continue
        gone, still, clean = 0, 0, 0
        detail = []
        for r in group:
            ok, err = replay(r["tool"], r["args"])
            if ok:
                gone += 1
                clean += 1
                continue
            if needle in err:
                still += 1
            else:
                gone += 1
                if r["args_ok"]:
                    detail.append("%s(%s) → %s" % (r["tool"], r["file"], err[:64]))
        full = sum(1 for r in group if r["args_ok"])
        print("\n[%s] 重放 %d 次（参数完整可判 %d / 截断仅判错误类型 %d）"
              % (label, len(group), full, len(group) - full))
        print("  原错误消失 %d / 仍报同类错 %d；其中参数完整者直接校验通过 %d"
              % (gone, still, clean))
        for t in detail[:6]:
            print("    仍被拦（转为其他错误）: %s" % t)

    print("\n其他 %d 次（本次未修，仅列分布）：" % len(other))
    seen = {}
    for r in other:
        key = r["err"][:60].replace("\n", " ")
        seen[key] = seen.get(key, 0) + 1
    for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
        print("  %2d× %s" % (v, k))


if __name__ == "__main__":
    main()
