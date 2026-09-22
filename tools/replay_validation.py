# -*- coding: utf-8 -*-
"""重放历史失败调用：验证 A/C 两类修复是否真能拦住当时那些错。

历史 trace 不会变，所以「修完重跑 err_recidivism.py 看下降」是无效判据——那读的是
同一份旧数据。真正可判的是：把当时那些 (tool, args) 拿新校验层原地重放一遍，看错误
还在不在。零 API 成本，不用等新 cycle。

判据（刻意分开报，不混成一个「修好了」）：
  A 类「尚未注册」——该错误与参数无关，属主能加载就该消失；参数被截断的记录也能判。
  参数完整的那部分另算「直接校验通过」，这才是「这一次本来能跑成」的证据。
  C 类「类型错」——必须参数完整才可判，截断的不计入。
"""
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
# 同目录兄弟模块（err_recidivism）：直接跑本脚本时脚本目录本就在 sys.path，
# 但被 importlib 按路径加载时不在——只靠 BASE 会 ModuleNotFoundError。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import err_recidivism as er  # noqa: E402
import harness  # noqa: E402
from tool_validation import find_tool_spec, normalize_arguments  # noqa: E402

# 真 _activate_skill 会写会话技能状态和 tools 流水——重放跑一次就往真实统计里塞假的
# 「激活」，后续观测（gene_fitness/tools_trace）全被污染。两个落盘副作用在这里掐掉。
agent._save_skills_state = lambda *a, **k: None
agent._trace_tools_change = lambda *a, **k: None

TRACES = os.path.join(BASE, "data", "longrun", "traces")
UNREGISTERED = "尚未注册"
# 判定串必须覆盖全部类型错原文，不能只认「期望数组」：search_batch 那条报的是
# 「期望 string，实际是 list」，漏掉它会把 5 次类型错算成 4 次 → self_iterate 的
# total<count 判据永远成立 → 该类 feasible 钉在 1.0，修好了也不退出选题。
TYPE_MISMATCH = ("期望数组", "类型不符")
# 类别归口不在此处自己写特征串，统一问 err_recidivism.classify ——
# 两处各写一套 needle 就会各算一套分母（历史上 C 类 9 vs 5），
# 而 self_iterate 的可行性判据直接拿两边数字比大小。
JUDGED_CLASSES = (("A", "技能未加载"), ("C", "参数类型错"), ("F", "必填参数缺失"))


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
    """钩子：重放端**不再**自己剥 arguments 信封。

    剥法只留运行时的 validate_arguments → normalize_arguments 那一份。重放端的职责
    是「拿当时的原始参数、走现在的校验层」，自己先剥一层就等于把要验证的修复抄进
    验证器里：读数会显示 gone，运行时却照旧报错（假读数）。原先这里的私有一份
    剥法只认 dict 内层，恰好也是 envelope-str 那 7 次一直没盖住的原因。
    """
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


def _row_shape(r: dict) -> str:
    """一行失败调用的参数形状，只用于报告（判定不看它）。

    envelope-* 是本轮修的形态：参数被包进单键 arguments（内层 dict 或 JSON 字符串）。
    """
    if not r["args_ok"]:
        return "truncated"
    args = r["args"]
    if isinstance(args, dict) and list(args.keys()) == ["arguments"]:
        return "envelope-dict" if isinstance(args["arguments"], dict) else "envelope-str"
    return "flat"


def _envelope_changed(r: dict) -> bool:
    """这行的参数是否**会被**本轮的信封归一改变。

    判据是机械的：把归一函数作用一遍，看参数是否真变了。变了 = 本轮修的机制对它
    生效（这类行如果重放仍报同类错，说明修复没起作用，必须拦住 feasible）；
    没变 = 模型传的是另一回事（如 code_read 收到 skill_name），校验层治不了它，
    不能拿它把 feasible 钉在 1.0——那正是历史上「修好了也退不出选题」的死循环。
    """
    args = r["args"]
    if not r["args_ok"] or not isinstance(args, dict):
        return False
    return normalize_arguments(None, args)[1] == "arguments_envelope"


def collect():
    """重放一次并汇总。文本输出与 --json 共用同一份读数，避免两处各算一遍。"""
    rows, shapes = load_failures()
    groups = {code: [] for code, _ in JUDGED_CLASSES}
    other = []
    for r in rows:
        code = er.classify(r["err"])
        if code in groups:
            groups[code].append(r)
        else:
            other.append(r)

    classes = {}
    for code, label in JUDGED_CLASSES:
        group = groups[code]
        gone, still, clean, still_fixable = 0, 0, 0, 0
        unjudgeable = 0
        detail = []
        for r in group:
            if not r["args_ok"]:
                # 参数被写入端截断（超长 content/old），无法还原 → 结构上判不了，
                # 既不记 still（会把 feasible 钉死）也不记 gone（那是假读数）。
                unjudgeable += 1
                continue
            ok, err = replay(r["tool"], r["args"])
            if ok:
                gone += 1
                clean += 1
                continue
            if er.classify(err) == code:
                still += 1
                if _envelope_changed(r):
                    still_fixable += 1
                continue
            gone += 1
            detail.append({"tool": r["tool"], "file": r["file"], "shape": _row_shape(r),
                           "err": err[:64]})
        classes[code] = {
            "label": label,
            "total": len(group),
            "args_full": sum(1 for r in group if r["args_ok"]),
            "gone": gone,
            "still": still,
            "still_fixable": still_fixable,
            "unjudgeable": unjudgeable,
            "clean": clean,
            "shapes": {s: sum(1 for r in group if _row_shape(r) == s)
                       for s in sorted({_row_shape(r) for r in group})},
            "detail": ["%s(%s)[%s] → %s" % (d["tool"], d["file"], d["shape"], d["err"])
                       for d in detail],
        }

    kinds = {}
    for r in other:
        key = r["err"][:60].replace("\n", " ")
        kinds[key] = kinds.get(key, 0) + 1
    return {
        "shapes": shapes,
        "failures": len(rows),
        "classes": classes,
        "other": {"total": len(other),
                  "kinds": sorted(kinds.items(), key=lambda kv: -kv[1])},
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0

    shapes = data["shapes"]
    cls = data["classes"]
    a, c, f = cls["A"], cls["C"], cls["F"]
    print("trace 参数形状：直接 %d / 包一层 %d / 截断不可还原 %d"
          % (shapes["flat"], shapes["wrapped"], shapes["truncated"]))
    print("失败调用 %d 次：A 技能未加载 %d / C 参数类型错 %d / F 必填参数缺失 %d / 其他 %d"
          % (data["failures"], a["total"], c["total"], f["total"], data["other"]["total"]))

    for code, label in JUDGED_CLASSES:
        k = cls[code]
        if not k["total"]:
            continue
        print("\n[%s] 重放 %d 次（参数完整可判 %d / 截断不可判 %d）"
              % (label, k["total"], k["args_full"], k["unjudgeable"]))
        print("  原错误消失 %d / 仍报同类错 %d（其中本机制可覆盖 %d）；参数完整者直接校验通过 %d"
              % (k["gone"], k["still"], k["still_fixable"], k["clean"]))
        if k["shapes"]:
            print("  参数形状：%s"
                  % "、".join("%s×%d" % (s, n) for s, n in sorted(k["shapes"].items())))
        for t in k["detail"][:6]:
            print("    仍被拦（转为其他错误）: %s" % t)

    print("\n其他 %d 次（本次未修，仅列分布）：" % data["other"]["total"])
    for k, v in data["other"]["kinds"]:
        print("  %2d× %s" % (v, k))
    return 0


if __name__ == "__main__":
    main()
