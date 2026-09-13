# -*- coding: utf-8 -*-
"""对话轮次效率指标 —— **纯观测**，只记录、不干预。

为什么单独成模块：
  1. 「提示词改得对不对」不能靠感觉。要判断一个提示词改动有没有让模型更愿意
     同轮并发、更少重复读文件，必须先有跨会话可对比的数字。这个模块就是那把尺。
  2. 与 agent.py 解耦：agent.py 已经 3800 行，再塞统计逻辑会淹没主流程；
     而且指标采集出错绝不该影响对话，隔离在这里能保证「观测失败 = 静默跳过」。

⚠ 边界（重要）：
  本模块**只做记录与聚合**。不自动调整任何策略、不注入上下文、不参与决策。
  （项目里曾有一套 execution_loop + efficiency.py 的「自学习闭环」——自动学策略
  并注入每轮请求——已被有意移除；本模块不是它的复活，只保留「量数据」这一层，
  因为那是评估优化效果的前提。）

数据落在 data/turn_metrics.jsonl，一行一轮，便于直接用 grep/pandas 看趋势。
"""
import json
import logging
import os
import time

logger = logging.getLogger("turn_metrics")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_PATH = os.path.join(BASE_DIR, "data", "turn_metrics.jsonl")

# 文件行数上限：超出后保留最近 KEEP 行。
# 指标文件只用于看趋势，留太久没有价值，反而拖慢读取。
MAX_LINES = 500
KEEP_LINES = 300


def _roll_if_needed() -> None:
    """文件过长时截断为最近 KEEP_LINES 行（重写一次，摊销后成本可忽略）。"""
    try:
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_LINES:
            return
        tmp = METRICS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP_LINES:])
        os.replace(tmp, METRICS_PATH)
    except Exception:
        pass


def record(goal: str, metrics: dict) -> None:
    """追加一条轮次指标。任何异常都吞掉——观测绝不能成为故障源。"""
    try:
        row = {"ts": time.time(), "goal": (goal or "")[:120]}
        row.update(metrics or {})
        os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _roll_if_needed()
    except Exception as e:
        logger.debug("轮次指标记录跳过: %s", e)

ERR_TRACE_PATH = os.path.join(BASE_DIR, "data", "tool_err_trace.jsonl")


def record_err(err_names, call_names=None, goal: str = "") -> None:
    """工具报错轮次的长期流水——**不滚动**，与 turn_metrics 的 300 行窗口分开存。

    为什么不能只靠 turn_metrics：那边 KEEP_LINES=300（实测 228 轮≈25 小时，即约
    1.4 天）会把历史吃掉，而「某条教训写入之后同类错误还犯不犯」要的是跨周窗口，
    滚动截断后无从对比。只在真有报错时写一行（实测 229 轮里 65 轮，约 28%）。
    异常照旧吞掉——观测绝不能成为故障源。
    """
    try:
        if not err_names:
            return
        row = {
            "ts": time.time(),
            "goal": (goal or "")[:60],
            "err_names": list(err_names),
            "call_names": list(call_names or []),
        }
        os.makedirs(os.path.dirname(ERR_TRACE_PATH), exist_ok=True)
        with open(ERR_TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("工具报错流水记录跳过: %s", e)


def load(limit: int = 0) -> list:
    """读回指标（默认全部）。损坏的行直接跳过，不抛异常。"""
    rows = []
    try:
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return rows[-limit:] if limit > 0 else rows


# 命中 token 的计价系数：实测命中价 = 非命中价的 1/50（用户确认，不是 1/10）。
# 它同时决定两件事：
#   1) 命中部分便宜 50 倍 → 把内容塞进「稳定前缀」极其划算；
#   2) 未命中（Δ，每次新增）几乎按全价计费 → Δ 的边际成本是 P 的 49 倍。
# 定价一变，结论会反转，所以写死在这里、并附在指标输出里，不允许漂移。
HIT_PRICE_RATIO = 0.02



def _avg(vals) -> float:
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else 0.0


def _fmt(n) -> str:
    """token 数的紧凑显示（与前端 fmtTokens 同口径，便于两边对照）。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return str(int(n))



def summarize(limit: int = 0) -> dict:
    """聚合指标，给出提示词优化最关心的几个派生量。

    关键派生量（也是判断提示词改动有没有用的依据）：
      calls_per_round  每轮平均发起几个工具调用 —— 模型**愿不愿意**同轮并发
      parallel_ratio   工具调用数 / 批次数 —— 同轮并发**实际**跑出多少并行
      re_read_rate     重复调用占比（轮内 + 跨轮）—— 白跑的比例
    前两个低 = 提示词没说服模型并发；第三个高 = 提示词没让它记住读过的内容。
    """
    rows = load(limit)
    rounds = len(rows)
    if not rounds:
        return {"rounds": 0}

    calls = [r.get("tool_calls", 0) or 0 for r in rows]
    batches = [r.get("batches", 0) or 0 for r in rows]
    re_reads = [r.get("re_reads", 0) or 0 for r in rows]
    cross = [r.get("cross_reads", 0) or 0 for r in rows]
    tool_rounds = [r.get("tool_rounds", 0) or 0 for r in rows]
    errs = [r.get("tool_errors", 0) or 0 for r in rows]
    merges = [r.get("mergeable_calls", 0) or 0 for r in rows]
    # 前缀缓存：命中率 = hit/(hit+miss)。注意它**与 prompt_tokens 无关**——
    # 有些提供方只给缓存口径不给一致的 prompt 字段，用 hit/(hit+miss) 才稳。
    cache_hit = sum(r.get("cache_hit", 0) or 0 for r in rows)
    cache_miss = sum(r.get("cache_miss", 0) or 0 for r in rows)
    # 命中率是**被轮次长短污染**的比值：一轮里 LLM 调用越多，首次冷启动的
    # miss 越被摊薄，看起来就越好。所以必须同时给出「与轮次无关」的稳态值：
    #   稳态命中率 = 1 - Δ/P，Δ = 每次调用新增（除首次冷启动）、P = 每次调用 prompt
    llm_calls = sum(r.get("llm_calls", 0) or 0 for r in rows)
    cold_miss = sum(r.get("cold_miss", 0) or 0 for r in rows)
    # Δ 的构成：本轮注入 messages 的工具结果字符数。拿它去对照 miss_per_call，
    # 就能验证「新增主要来自工具结果」这个假设——而不是凭感觉断定。
    tool_chars = sum(r.get("tool_chars", 0) or 0 for r in rows)
    # ---- Δ 逐次归因：把「新增花在哪」拆到每一次 LLM 调用 ----
    # 轮次总量只能告诉我们 Δ 多大，归因才能告诉我们该改哪里。
    _trace = [c for r in rows for c in (r.get("call_trace") or [])]
    _warm = [c for c in _trace if (c.get("d") or 0) > 0]   # 首次调用是冷启动，无前序
    trace_delta = sum(c.get("d") or 0 for c in _warm)
    trace_chars = sum(c.get("chars") or 0 for c in _warm)
    # 隐含「1 字符 = 多少 token」。中英混排/代码正常落在 0.25~1.0；
    # 明显超出 1 说明 Δ 另有来源（不是我们追加进 messages 的内容）——
    # 那就得去查别的地方，而不是去砍工具结果上限。
    char_token_ratio = round(trace_delta / trace_chars, 3) if trace_chars else 0.0
    top_deltas = sorted(((c.get("d") or 0, c.get("chars") or 0) for c in _warm),
                        reverse=True)[:3]
    # 跨轮前缀是否被整段改写：新一轮首次调用的 prompt 里有多大比例未命中。
    # 稳态命中率 94% 的会话，只要跨轮前缀被改写，每轮就要白烧一次全价前缀——
    # 这是单点最大浪费，而且与「每轮新增多少」完全无关。
    first_prompt = (_trace[0].get("p") or 0) if _trace else 0
    cold_ratio = round(cold_miss / first_prompt, 3) if first_prompt else 0.0
    # ---- 首条消息（sys_prompt）指纹：唯一必须跨轮逐字节恒定的消息 ----
    # 它一变，其后全部内容（含整段历史）全废。实测踩过：模式切换时它跟着变
    # （工程模式少 137 字符外观说明），跨轮命中只剩 1,664 token ≈ 首条本身。
    # 所以「sys 指纹变化次数」是首条稳定性的直接判据，比总量指标更早报警。
    _sys_fps = [str((r.get("seg") or {}).get("sys_md5") or "") for r in rows]
    _sys_fps = [h for h in _sys_fps if h]
    sys_changes = sum(1 for a, b in zip(_sys_fps, _sys_fps[1:]) if a != b)
    sys_chars = ((rows[-1].get("seg") or {}).get("sys") or 0) if rows else 0
    # ---- 跨轮前缀断点：miss 落在哪一段（决定该修哪里）----
    _pfx = [r.get("prefix") or {} for r in rows if r.get("prefix")]
    _pfx_warm = [p for p in _pfx if not p.get("cold")]
    last_prefix = _pfx_warm[-1] if _pfx_warm else (_pfx[-1] if _pfx else {})
    break_labels = {}
    for p in _pfx_warm:
        k = p.get("break_label") or "?"
        # history[0..9] 归类为前段：断在前段 = 窗口在滑动（整段历史报废），
        # 断在后段 = 正常追加（只报废尾巴）。两者修法完全不同。
        if k.startswith("history["):
            try:
                k = "history[前段]" if int(k[8:-1]) <= 9 else "history[后段]"
            except ValueError:
                pass
        break_labels[k] = break_labels.get(k, 0) + 1
    tools_changed = sum(1 for p in _pfx_warm
                        if p.get("tools_changed") or (p.get("tools_diff") or {}).get("any"))
    wasted_chars = sum(p.get("wasted_chars") or 0 for p in _pfx_warm)
    # 固定前缀构成：tools 排在请求最前面，它的大小决定「每轮白付多少全价前缀」，
    # 它的变化决定「整条前缀会不会全废」。所以必须单独报，不能埋在总 prompt 里。
    _last_row = rows[-1] if rows else {}
    tools_count = _last_row.get("tools_count") or 0
    tools_chars = _last_row.get("tools_chars") or 0
    last_mode = str(_last_row.get("mode") or "")
    prompt_total = sum(r.get("prompt_tokens", 0) or 0 for r in rows)
    prompt_per_call = (prompt_total / llm_calls) if llm_calls else 0.0
    miss_per_call = (cache_miss / llm_calls) if llm_calls else 0.0
    # 去掉首次冷启动后的「每次调用新增 miss」
    warm_calls = sum(max(0, (r.get("llm_calls", 0) or 0) - 1) for r in rows)
    warm_miss = max(0, cache_miss - cold_miss)
    steady_hit = (round(1 - (warm_miss / warm_calls) / prompt_per_call, 4)
                  if warm_calls and prompt_per_call else None)
    # ---- 等效成本：真正的目标函数（命中率是它的近似，不是目标）----
    # 计费 = hit×HIT_PRICE_RATIO + miss×1.0（命中价 = 非命中的 1/50）。
    # 代入 hit = P - miss：
    #     成本 = 0.02×P + 0.98×Δ      （Δ = 本次调用新增的 token）
    # 关键结论：**Δ 的边际成本是 P 的 49 倍**——Δ 几乎按全价计费，
    # 所以省钱只有一个方向：把 Δ 压下去；而命中率 = 1 - Δ/P 是混淆指标，
    # 把 P 撑大同样能把命中率从 68% 抬到 96%，但成本只增不减（P 系数虽小仍为正）。
    equiv_cost = cache_hit * HIT_PRICE_RATIO + cache_miss
    delta_per_call = (warm_miss / warm_calls) if warm_calls else 0.0
    steady_cost = (prompt_per_call * HIT_PRICE_RATIO
                   + (1 - HIT_PRICE_RATIO) * delta_per_call)
    # 「命中率 99%」的代价参考：Δ = 0.01P 时成本 = 0.02P + 0.98×0.01P = 0.0298×P。
    # 拿它跟当前稳态成本比，就能看出「追 99%」到底是省钱还是烧钱。
    cost_at_99 = (prompt_per_call * (HIT_PRICE_RATIO + 0.01 * (1 - HIT_PRICE_RATIO))
                  if prompt_per_call else 0.0)
    # 上下文分段均值：用于定位「命中率为什么上不去」——是静态前缀太小、
    # 还是易变尾巴太大、还是历史在每轮重建。
    _segs = [r.get("seg") or {} for r in rows if r.get("seg")]
    # 前缀全废检测：sys_prompt 指纹一变（技能激活/淘汰、人设切换、模式切换），
    # 整条前缀（约 P token）全部 miss——代价 ≈ 0.98P，相当于十几次稳态调用。
    # 这是命中率的「断崖」，比逐轮新增更能解释命中率的抖动：稳态 94% 的会话
    # 只要中间激活一次技能，整轮数字就会掉到 60% 以下。
    _md5s = [(r.get("seg") or {}).get("sys_md5") for r in rows]
    prefix_breaks = sum(1 for _a, _b in zip(_md5s, _md5s[1:]) if _a and _b and _a != _b)
    break_cost = int(prefix_breaks * prompt_per_call * (1 - HIT_PRICE_RATIO))
    _seg_keys = ("sys", "summary", "memory", "hist", "dyn", "recall", "work", "total")
    seg_avg = ({k: int(_avg([x.get(k, 0) or 0 for x in _segs])) for k in _seg_keys}
               if _segs else {})

    total_calls = sum(calls)
    total_batches = sum(batches)
    total_re = sum(re_reads) + sum(cross)

    # 只统计「调了工具、且有批次记录」的轮次：
    #   · 纯聊天轮（0 调用）会把均值稀释，看不出模型的真实并发意愿；
    #   · batches=0 的轮（文本协议路径不走分批调度）没有并行度可比，
    #     混进来会把 parallel_ratio 算高——那是口径偏差，不是真的更快。
    active = [(c, b) for c, b in zip(calls, batches) if c > 0 and b > 0]
    active_calls = sum(c for c, _ in active)
    active_batches = sum(b for _, b in active if b > 0)

    return {
        "rounds": rounds,
        "tool_rounds_avg": _avg(tool_rounds),
        "calls_per_round": round(active_calls / len(active), 2) if active else 0.0,
        "parallel_ratio": (round(active_calls / active_batches, 2)
                           if active_batches else 0.0),
        "tool_calls_total": total_calls,
        "re_reads_total": sum(re_reads),
        "cross_reads_total": sum(cross),
        "re_read_rate": (round(total_re / total_calls, 4) if total_calls else 0.0),
        "duration_s_avg": round(_avg([r.get("duration_ms", 0) or 0 for r in rows]) / 1000, 1),
        # 「整轮成功」这个指标原来用的是「本轮有无任何工具报错」——口径过严：
        # 一轮里 grep 没匹配到、命令返回非零退出码，整轮就被记为失败，
        # 但用户看到的结果其实是成功的。改为两个诚实的量：
        #   tool_errors_total   本轮工具报错次数（可定位到具体轮次）
        #   clean_rounds_rate   全程无一次工具报错的轮次占比
        "tool_errors_total": sum(errs),
        "clean_rounds_rate": (round(sum(1 for e in errs if e == 0) / rounds, 3)),
        # 可合并调用：同一工具在一轮里被拆成多次调用、而它本来支持批量参数。
        # 这是「并行度为什么上不去」的直接度量——调度器只能并行「已经发出来的
        # 多个调用」，而真正的浪费是「本该 1 次却发了 N 次」，调度器无法挽救。
        "mergeable_total": sum(merges),
        "mergeable_rate": (round(sum(merges) / total_calls, 4) if total_calls else 0.0),
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        "cache_hit_rate": (round(cache_hit / (cache_hit + cache_miss), 4)
                           if (cache_hit + cache_miss) else None),
        "llm_calls": llm_calls,
        "cold_miss": cold_miss,
        "tool_chars_total": tool_chars,
        "tool_chars_per_call": int(tool_chars / llm_calls) if llm_calls else 0,
        "traced_calls": len(_trace),
        "trace_delta": trace_delta,
        "trace_chars": trace_chars,
        "char_token_ratio": char_token_ratio,
        "top_deltas": top_deltas,
        "first_prompt": first_prompt,
        "cold_ratio": cold_ratio,
        "last_prefix": last_prefix,
        "break_labels": break_labels,
        "tools_changed": tools_changed,
        "wasted_chars": wasted_chars,
        "tools_count": tools_count,
        "tools_chars": tools_chars,
        "last_mode": last_mode,
        "sys_chars": sys_chars,
        "sys_changes": sys_changes,
        "prompt_total": prompt_total,
        "prompt_per_call": int(prompt_per_call),
        "miss_per_call": int(miss_per_call),
        "steady_hit_rate": steady_hit,
        "equiv_cost": int(equiv_cost),
        "equiv_cost_per_call": int(equiv_cost / llm_calls) if llm_calls else 0,
        "delta_per_call": int(delta_per_call),
        "steady_cost_per_call": int(steady_cost),
        "cost_at_99_hit": int(cost_at_99),
        "prefix_breaks": prefix_breaks,
        "prefix_break_cost": break_cost,
        "seg_avg": seg_avg,
    }


def format_summary(limit: int = 0) -> str:
    """人类可读的摘要（供命令行/汇报使用）。"""
    s = summarize(limit)
    if not s.get("rounds"):
        return "暂无轮次指标（data/turn_metrics.jsonl 为空）"
    _hr = s.get("cache_hit_rate")
    _hr_txt = ("%.1f%%  （hit %s / miss %s）" % (_hr * 100, _fmt(s["cache_hit"]), _fmt(s["cache_miss"]))
               if _hr is not None else "无数据（提供方未返回缓存口径）")
    _calls = s.get("llm_calls") or 0
    _sh = s.get("steady_hit_rate")
    _cache_txt = (
        "  前缀缓存命中率  %s\n"
        "      LLM 调用 %d 次，每次 prompt %s / 新增 miss %s\n"
        "      首次冷启动 miss %s（占 miss 的 %.0f%%）← 重启/换前缀导致，与轮次长短无关\n"
        "      稳态命中率    %s   ← 1 - 新增/prompt，与轮次长短无关，用于横向对比\n"
        % (_hr_txt, _calls, _fmt(s.get("prompt_per_call", 0)),
           _fmt(s.get("miss_per_call", 0)), _fmt(s.get("cold_miss", 0)),
           (s.get("cold_miss", 0) * 100.0 / max(1, s.get("cache_miss", 1))),
           ("%.1f%%" % (_sh * 100)) if _sh is not None else "—"))
    # 等效成本才是目标函数。命中率可以靠「把上下文撑大」刷上去，成本却会变贵；
    # 拿「命中率 99% 的成本」与「当前稳态成本」并列，一眼看出追 99% 是省钱还是烧钱。
    _sc = s.get("steady_cost_per_call") or 0
    _c99 = s.get("cost_at_99_hit") or 0
    _cmp = ""
    if _sc and _c99:
        _cmp = ("      → 追 99%% 的成本 %s/次，当前稳态 %s/次（%s%.0f%%）\n"
                % (_fmt(_c99), _fmt(_sc), "省 " if _c99 < _sc else "贵 ",
                   abs(_c99 - _sc) * 100.0 / _sc))
    # Δ 归因：只报「Δ 多大」没用，要报「Δ 花在哪」。隐含字符→token 比例是
    # 一个自证指标：正常文本 0.25~1.0；超出说明 Δ 另有来源，不该去砍工具结果。
    _ratio = s.get("char_token_ratio") or 0
    _attr_txt = ""
    if s.get("traced_calls"):
        _tops = "，".join("%s token/%s 字符" % (_fmt(_d), _fmt(_c))
                          for _d, _c in (s.get("top_deltas") or []))
        _attr_txt = (
            "      Δ 归因：%d 次调用追加 %s 字符 / Δ 合计 %s token"
            "（1 字符 ≈ %.2f token）\n"
            "          最大的三次新增：%s\n"
            % (s.get("traced_calls", 0), _fmt(s.get("trace_chars", 0)),
               _fmt(s.get("trace_delta", 0)), _ratio, _tops))
        if _ratio > 1.2:
            _attr_txt += ("          注意：比例异常偏高 → Δ 另有来源（不是追加的内容），"
                          "先查它，别急着砍工具结果上限\n")
    # 跨轮前缀：若首次调用就有大半未命中，说明前缀被整段改写（历史窗口移位、
    # 系统提示词变化、记忆块重排）——每轮白烧一次全价前缀，比 Δ 更值得先修。
    _cold_txt = ""
    if s.get("first_prompt"):
        _cr = s.get("cold_ratio") or 0
        _cold_txt = ("      跨轮前缀：首次调用 prompt %s，未命中 %s（%.0f%%）%s\n"
                     % (_fmt(s["first_prompt"]), _fmt(s.get("cold_miss", 0)),
                        _cr * 100,
                        "← 前缀被整段改写，单点最大浪费" if _cr > 0.5 else ""))
    # 断点位置决定修法：断在 tools → 技能工具集不稳；断在 history 前段 → 窗口在滑动；
    # 断在末尾附近 → 正常追加（健康）。所以不能只报「miss 多大」。
    _brk_txt = ""
    _lp = s.get("last_prefix") or {}
    if _lp:
        _td = _lp.get("tools_diff") or {}
        _td_txt = ""
        if _td.get("any"):
            _bits = []
            if _td.get("added"):
                _bits.append("新增 %d 个（+%s 字符）"
                             % (len(_td["added"]), _fmt(_td.get("added_chars") or 0)))
            if _td.get("removed"):
                _bits.append("移除 %d 个" % len(_td["removed"]))
            if _td.get("changed"):
                _bits.append("定义变化 %d 个" % len(_td["changed"]))
            if _td.get("order_changed"):
                _bits.append("顺序变化（%d→%d 个）"
                             % (_td.get("prev_count"), _td.get("cur_count")))
            _td_txt = "；工具集变化→" + "、".join(_bits) + "（tools 在最前，整条前缀失效）"
        if _lp.get("cold"):
            _brk_txt = ("      跨轮断点：无上轮状态（重启后首轮），本轮全量计费；"
                        "tools %s 个 / %s 字符\n"
                        % (_lp.get("tools_count"), _fmt(_lp.get("tools_chars") or 0)))
        else:
            _brk_txt = ("      跨轮断点：第 %s 条（%s），前 %s 条可复用；"
                        "上轮断点后 %s 字符作废%s\n"
                        % (_lp.get("break_at"), _lp.get("break_label"),
                           _lp.get("keep_msgs"), _fmt(_lp.get("wasted_chars") or 0),
                           _td_txt))
            if _lp.get("break_cur_head"):
                _brk_txt += ("          新内容开头：%s ｜ 上轮同位置：%s\n"
                             % (_lp.get("break_cur_head"),
                                _lp.get("break_prev_head") or "（无）"))
    if s.get("break_labels"):
        _brk_txt += ("      断点分布（%d 轮）：%s%s\n"
                     % (sum(s["break_labels"].values()),
                        "，".join("%s×%d" % (_k, _v)
                                 for _k, _v in sorted(s["break_labels"].items(),
                                                      key=lambda kv: -kv[1])),
                        "；工具集变化 %d 次" % s["tools_changed"]
                        if s.get("tools_changed") else ""))
    _cost_txt = (
        "  等效输入成本    %s/次  ← 真正的目标函数：0.02×prompt + 0.98×新增\n"
        "      累计等效 %s（命中×0.02 + 未命中×1.0，命中价 = 非命中的 1/50）\n"
        "      注意：新增的边际成本是 prompt 的 49 倍 → 优先砍新增，不是撑大 prompt\n"
        "      新增来源：工具结果注入 %s 字符/次调用（本轮共 %s）"
        "（是否为主来源，看下面的归因）\n"
        "%s"
        "%s"
        "%s"
        "      固定前缀 tools %s 个 / %s 字符（本轮模式 %s）← 在最前面，变了全废\n"
        "      首条消息 sys_prompt %s 字符，跨轮指纹变化 %d 次 ← 它一变后面全废\n"
        "      前缀全废 %d 次（技能激活/人设切换会换 sys_prompt 指纹）"
        "← 额外成本 %s，相当于 %.1f 次稳态调用\n"
        "%s"
        % (_fmt(_sc), _fmt(s.get("equiv_cost", 0)),
           _fmt(s.get("tool_chars_per_call", 0)), _fmt(s.get("tool_chars_total", 0)),
           _attr_txt, _cold_txt, _brk_txt,
           s.get("tools_count", 0), _fmt(s.get("tools_chars", 0)),
           s.get("last_mode") or "?",
           _fmt(s.get("sys_chars", 0)), s.get("sys_changes", 0),
           s.get("prefix_breaks", 0), _fmt(s.get("prefix_break_cost", 0)),
           (s.get("prefix_break_cost", 0) / _sc) if _sc else 0.0,
           _cmp)
        if _sc else "  等效输入成本    无数据（缺 llm_calls，需重启后跑一轮）\n")
    _sg = s.get("seg_avg") or {}
    _sg_txt = ("静态 %d / 历史 %d / 易变尾巴 %d（动态 %d+召回 %d+最近执行 %d）字符"
               % (_sg.get("sys", 0), _sg.get("hist", 0),
                  _sg.get("dyn", 0) + _sg.get("recall", 0) + _sg.get("work", 0),
                  _sg.get("dyn", 0), _sg.get("recall", 0), _sg.get("work", 0))
               if _sg else "无数据")
    return (
        "轮次指标（最近 %d 轮）\n"
        "  每轮工具调用数  %.2f   ← 模型愿不愿意同轮并发\n"
        "  同轮并行度      %.2f   ← 实际跑出多少并行（1.0 = 全串行）\n"
        "  重复调用占比    %.1f%%  （轮内 %d 次 / 跨轮 %d 次）\n"
        "  平均工具轮数    %.1f\n"
        "  平均耗时        %.1f s\n"
        "  无报错轮次占比  %.0f%%  （工具报错共 %d 次）\n"
        "  可合并调用      %d 次（占调用 %.1f%%）← 本该 1 次却拆成多次\n"
        "%s"
        "%s"
        "  上下文分段均值  %s\n"
        % (s["rounds"], s["calls_per_round"], s["parallel_ratio"],
           s["re_read_rate"] * 100, s["re_reads_total"], s["cross_reads_total"],
           s["tool_rounds_avg"], s["duration_s_avg"],
           s["clean_rounds_rate"] * 100, s["tool_errors_total"],
           s["mergeable_total"], s["mergeable_rate"] * 100,
           _cost_txt, _cache_txt, _sg_txt)
    )


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(format_summary())
