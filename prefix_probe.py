"""前缀断点探针：定位「跨轮首次调用」的缓存是从哪一条消息开始失效的。

为什么需要它：实测每轮首次调用的未命中量约等于整个首条 prompt（命中只有 1.6k
token，miss 60k+），也就是**跨轮几乎零复用**。但聚合指标只说得出「miss 多大」，
说不出「从哪断的」。而修法完全取决于断点位置：

  · tools 数组变化      → 技能激活/淘汰，或工具顺序被 set 迭代打乱（在最前面，全废）
  · 第 0 条 sys_prompt  → 人设/技能摘要/模式改了首条消息
  · 第 1 条             → 模式姿态块（工程/日常）在跨轮切换
  · history 第 0 条     → 历史窗口在滑动，整段历史每轮报废
  · history 末尾附近    → 正常追加，只报废尾巴（健康）
  · 易变尾巴            → 尾巴排在 history 之后，报废范围被限制住（健康）

做法：每轮首次调用前，把 messages 逐条做内容指纹，与上一轮**最后一次调用**的
指纹逐条比对，找到第一个不同的下标；配合构造时打的分段标记，输出断点落在哪一段。
状态落在 data/prefix_state.json（只存指纹与片段，不存正文）。

纯观测：不参与任何决策，异常一律吞掉。
"""
import hashlib
import json
import logging
import os

logger = logging.getLogger("prefix_probe")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "prefix_state.json")


def _h(text: str) -> str:
    return hashlib.md5(str(text).encode("utf-8", "ignore")).hexdigest()[:8]


def _msg_repr(m) -> str:
    """消息的规范化表示：role + content + tool_calls 全参与，顺序稳定。"""
    if not isinstance(m, dict):
        return str(m)
    return json.dumps({
        "role": m.get("role") or "",
        "content": m.get("content") if m.get("content") is not None else "",
        "tool_calls": m.get("tool_calls") or None,
        "tool_call_id": m.get("tool_call_id") or None,
    }, ensure_ascii=False, sort_keys=True)


def _msg_head(m) -> str:
    """人看的片段：断点落在哪一条时，一眼认出那是什么块。"""
    if not isinstance(m, dict):
        return str(m)[:48]
    c = m.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip().replace("\n", " ")[:48]
    if m.get("tool_calls"):
        return "[tool_calls] " + json.dumps(m.get("tool_calls"), ensure_ascii=False)[:36]
    return "[%s]" % (m.get("role") or "?")


def _tool_entry(t):
    fn = (t or {}).get("function") or {}
    name = str(fn.get("name") or "")
    body = json.dumps(t, ensure_ascii=False, sort_keys=True)
    return [name, len(body), _h(body)]


def fingerprint(messages, tools=None):
    msgs = [_msg_repr(m) for m in (messages or [])]
    tl = [_tool_entry(t) for t in (tools or [])]
    tj = json.dumps(tl, ensure_ascii=False)
    return {
        "msgs": [_h(s) for s in msgs],
        "chars": [len(s) for s in msgs],
        "heads": [_msg_head(m) for m in (messages or [])],
        "tools": _h(tj) if tools else "",
        "tools_chars": sum(e[1] for e in tl),
        "tools_list": tl,
    }


def tools_diff(prev, cur):
    """工具集差异。tools 排在请求最前面，它一变后面全废——所以单独拆出来报。

    上一轮状态若没存过工具清单（旧版本落盘的状态文件），不能把它当成
    「工具全被移除」——宁可报「未知」，也不要给一个错的结论。
    """
    if "tools_list" not in prev:
        return {"any": False, "unknown": True, "added": [], "removed": [],
                "changed": [], "order_changed": False, "added_chars": 0,
                "removed_chars": 0, "prev_count": 0,
                "cur_count": len(cur.get("tools_list") or [])}
    pm = {n: (sz, h) for n, sz, h in (prev.get("tools_list") or [])}
    cm = {n: (sz, h) for n, sz, h in (cur.get("tools_list") or [])}
    added = sorted(set(cm) - set(pm))
    removed = sorted(set(pm) - set(cm))
    changed = sorted(n for n in set(pm) & set(cm) if pm[n] != cm[n])
    porder = [n for n, _, _ in (prev.get("tools_list") or [])]
    corder = [n for n, _, _ in (cur.get("tools_list") or [])]
    order_changed = bool(porder or corder) and porder != corder
    d = {
        "added": added,
        "removed": removed,
        "changed": changed,
        "order_changed": order_changed,
        "added_chars": sum(cm[n][0] for n in added),
        "removed_chars": sum(pm[n][0] for n in removed),
        "prev_count": len(porder),
        "cur_count": len(corder),
    }
    d["any"] = bool(added or removed or changed or order_changed)
    return d


def _label_at(idx, labels, marks):
    """把消息下标翻译成「断在哪一段」——这是整个探针的输出价值所在。"""
    if labels and idx < len(labels) and labels[idx]:
        return labels[idx]
    if not marks:
        return "?"
    hist = marks.get("history")
    tail = marks.get("tail")
    if idx == 0:
        return "sys_prompt（首条就变了：人设/技能摘要/模式）"
    if hist is not None and idx < hist:
        if marks.get("memory") is not None and idx >= marks["memory"]:
            return "memory_block（常驻记忆）"
        if marks.get("summary") is not None and idx >= marks["summary"]:
            return "summary_block（长期摘要）"
        return "mode/sys 层（第 %d 条）" % idx
    if hist is not None and tail is not None and hist <= idx < tail:
        return "history[%d]（共 %d 条）" % (idx - hist, tail - hist)
    if tail is not None and idx == tail:
        return "history 末尾（新一轮起点：上轮尾巴从此处开始，全作废）"
    if tail is not None and idx > tail:
        return "上轮易变尾巴/轮内消息 +%d" % (idx - tail)
    return "?"


def compare(prev, cur, labels=None, marks=None):
    """逐条比对指纹，返回断点报告。"""
    pm, cm = prev.get("msgs") or [], cur.get("msgs") or []
    keep = 0
    for a, b in zip(pm, cm):
        if a != b:
            break
        keep += 1
    pc, cc = prev.get("chars") or [], cur.get("chars") or []
    ph, ch = prev.get("heads") or [], cur.get("heads") or []
    broke = keep < len(cm)
    rep = {
        "tools_diff": tools_diff(prev, cur),
        "tools_chars": cur.get("tools_chars") or 0,
        "tools_count": len(cur.get("tools_list") or []),
        "keep_msgs": keep,
        "prev_msgs": len(pm),
        "cur_msgs": len(cm),
        "break_at": keep if broke else -1,
        "break_label": _label_at(keep, labels, marks) if broke else "无（完全前缀）",
        "break_prev_head": ph[keep] if broke and keep < len(ph) else "",
        "break_cur_head": ch[keep] if broke and keep < len(ch) else "",
        "break_prev_chars": pc[keep] if broke and keep < len(pc) else 0,
        "break_cur_chars": cc[keep] if broke and keep < len(cc) else 0,
        "keep_chars": sum(cc[:keep]),
        "cur_chars": sum(cc),
        # 上一轮在断点之后的内容全部报废：这才是「白烧掉多少缓存」的量
        "wasted_chars": sum(pc[keep:]),
        "hist_marks": dict(marks or {}),
    }
    # 跨会话识别：状态文件是全局单文件、不按会话存——新会话首轮会拿上一会话
    # 末尾状态做比对，于是每开一次新对话就报一次「N 个工具被移除」的假警。
    # 判据只看 keep：同会话续轮时历史整段保留（keep 通常 10+），keep∈(0,2] 只可能
    # 是新会话。曾用「且 len(cm)<len(pm)」做保守条件，漏判 4 轮（新会话首轮就跑完
    # 多轮工具、消息数反而更多）。实测 184 轮：keep<=2 共 90 轮，其中 tools_diff.any
    # 53 轮是假警，真·同会话工具变化只剩 2 轮（1 次真重置、1 次描述变化）。
    # 跨会话本来就没有可复用的前缀，工具差异不算「轮内变化」：只标事实，不改数字。
    rep["cross_session"] = bool(0 < keep <= 2)
    # 归因一句话：先看 tools（在最前面，最致命），再看断点位置
    if rep["cross_session"]:
        rep["cause"] = "跨会话（新对话：上轮状态属另一个会话，工具差异不计为轮内变化）"
    elif rep["tools_diff"]["any"]:
        rep["cause"] = "tools（工具集变化 → 整条前缀失效）"
    elif not broke:
        rep["cause"] = "无（前缀完全一致）"
    elif keep == 0:
        rep["cause"] = "sys_prompt（首条消息变了）"
    else:
        rep["cause"] = rep["break_label"]
    return rep


def report(messages, tools=None, labels=None, marks=None):
    """在「本轮首次调用前」调用：返回与上一轮最后状态的比对结果。"""
    try:
        cur = fingerprint(messages, tools)
        rep = {"cold": True}
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                if prev.get("msgs"):
                    rep = compare(prev, cur, labels, marks)
                    rep["cold"] = False
            except Exception as e:
                logger.debug("读取前缀状态失败（按冷启动处理）: %s", e)
        rep["cur_msgs"] = len(cur["msgs"])
        rep["cur_chars"] = sum(cur["chars"])
        rep.setdefault("tools_chars", cur.get("tools_chars") or 0)
        rep.setdefault("tools_count", len(cur.get("tools_list") or []))
        return rep
    except Exception as e:
        logger.debug("前缀探针失败（忽略）: %s", e)
        return {}


def save(messages, tools=None, labels=None):
    """在本轮结束时调用：把「最后一次调用」的状态存起来，供下一轮比对。"""
    try:
        fp = fingerprint(messages, tools)
        fp["labels"] = list(labels or [])[:len(fp["msgs"])]
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(fp, f, ensure_ascii=False)
        return True
    except Exception as e:
        logger.debug("保存前缀状态失败（忽略）: %s", e)
        return False


def describe(rep) -> str:
    """把断点报告变成一句人话（供摘要/日志用）。"""
    if not rep:
        return ""
    if rep.get("cold"):
        return ("跨轮断点：无上轮状态（首次/重启后），本轮按全量计费；"
                "工具 %s 个 / %s 字符"
                % (rep.get("tools_count"), rep.get("tools_chars")))
    parts = []
    td = rep.get("tools_diff") or {}
    if td.get("any") and not rep.get("cross_session"):
        seg = []
        if td.get("added"):
            seg.append("新增 %d 个（+%s 字符）：%s"
                       % (len(td["added"]), td.get("added_chars"),
                          "、".join(td["added"][:4])))
        if td.get("removed"):
            seg.append("移除 %d 个：%s" % (len(td["removed"]),
                                           "、".join(td["removed"][:4])))
        if td.get("changed"):
            seg.append("定义变化 %d 个：%s" % (len(td["changed"]),
                                               "、".join(td["changed"][:4])))
        if td.get("order_changed"):
            seg.append("顺序变化（%d→%d 个）" % (td.get("prev_count"), td.get("cur_count")))
        parts.append("工具集变化 → " + "；".join(seg))
    parts.append("断点在第 %s 条（%s），前 %d 条可复用 %s 字符；"
                 "上轮断点后 %s 字符作废"
                 % (rep.get("break_at"), rep.get("break_label"),
                    rep.get("keep_msgs", 0), rep.get("keep_chars", 0),
                    rep.get("wasted_chars", 0)))
    if rep.get("break_cur_head"):
        parts.append("新内容开头：%s（上轮同位置：%s）"
                     % (rep.get("break_cur_head"), rep.get("break_prev_head")))
    return "跨轮断点：" + "；".join(parts)
