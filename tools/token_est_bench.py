# -*- coding: utf-8 -*-
"""estimate_tokens 提速基准：Python 实现优化 vs 搬到 Node(TS) 的 IPC 代价。

背景：estimate_tokens 是每轮工具循环的热路径函数（对全部消息反复重算）。
本基准回答一个问题——「把它改成 TS 跑在 Node 上，到底能不能提速」。

三条路线对照：
  1. py_old  : 当前 memory.estimate_tokens（findall + 两次 sub + py 循环）
  2. py_fast : 纯 Python 等价改写（len 相减数 CJK + split 数空白，少建两份大 list）
  3. node_ipc: V8 同算法，但走常驻 worker 的 JSON+管道往返（与 server.py
               _ts_transpile 完全相同的 IPC 模式）

验收标准：
  - py_fast 与 py_old 在全部语料上逐条完全一致（差异必须为 0）
  - node_ipc 同时报「计算耗时」与「含 IPC 的总耗时」，以及数值差异数

用法：venv/bin/python tools/token_est_bench.py
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from memory import estimate_tokens as py_mem, _CJK_RE, _WORD_RE  # noqa: E402

_WS_RE = re.compile(r"\s")  # 仅旧实现对照用（memory.py 已改用 str.split 计数）


def py_old(text: str) -> int:
    """冻结的旧实现（对照基准）：改 memory.py 后仍可回归。"""
    if not text:
        return 0
    s = str(text)
    cjk = len(_CJK_RE.findall(s))
    rest = _CJK_RE.sub("", s)
    words = _WORD_RE.findall(rest)
    rest_no_words = _WORD_RE.sub("", rest)
    punct = len(_WS_RE.sub("", rest_no_words))
    est = cjk + sum((len(w) + 3) // 4 for w in words) + (punct + 1) // 2
    return int(est * 1.1) + 1


def py_fast(text: str) -> int:
    """等价改写：CJK 数用 len 相减（CJK 都是单字符），空白数用 str.split()。"""
    if not text:
        return 0
    s = str(text)
    rest = _CJK_RE.sub("", s)
    cjk = len(s) - len(rest)                       # 省掉 findall 建 list
    words = _WORD_RE.findall(rest)
    rest_no_words = _WORD_RE.sub("", rest)
    punct = sum(map(len, rest_no_words.split()))
    est = cjk + sum((len(w) + 3) // 4 for w in words) + (punct + 1) // 2
    return int(est * 1.1) + 1


NODE_JS = r"""
const CJK=/[\u4e00-\u9fff\u3400-\u4dbf]/g, W=/[A-Za-z0-9]+/g, WS=/\s/g;
function est(text){
  if(!text) return 0;
  const s = String(text);
  const cjk = (s.match(CJK)||[]).length;
  const rest = s.replace(CJK,'');
  const words = rest.match(W)||[];
  const restNoWords = rest.replace(W,'');
  const punct = restNoWords.replace(WS,'').length;
  let e = cjk;
  for(const w of words) e += Math.floor((w.length+3)/4);
  e += Math.floor((punct+1)/2);
  return Math.floor(e*1.1)+1;
}
let buf='';
process.stdin.on('data', d=>{
  buf += d.toString('utf8');
  let i;
  while((i = buf.indexOf('\n')) >= 0){
    const line = buf.slice(0,i); buf = buf.slice(i+1);
    if(!line) continue;
    let out;
    try { out = {n: est(JSON.parse(line).text)}; }
    catch(e){ out = {err: String(e)}; }
    process.stdout.write(JSON.stringify(out)+'\n');
  }
});
"""


def build_corpus():
    """真实规模语料：单条 200k 字符 + 一轮 200 条消息（约 400k 字符）。"""
    src = ""
    for p in ("memory.py", "agent.py", "HARNESS.md", "server.py"):
        fp = os.path.join(BASE, p)
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8", errors="replace") as f:
                src += f.read()
    chat = ("这是大白的一轮对话消息，包含中文说明、English words 12345、"
            "标点符号（括号）、以及 emoji 🎉 与代码 `x=1`。\n") * 200
    blob = (chat + src) * 3
    single = blob[:200000]
    msgs = [single[i:i + 2000] for i in range(0, 400000, 2000)]
    return single, msgs


def timeit(fn, arg, rounds=3):
    best = float("inf")
    for _ in range(rounds):
        t = time.perf_counter()
        fn(arg)
        best = min(best, (time.perf_counter() - t) * 1000)
    return best


def main():
    single, msgs = build_corpus()
    total_chars = len(single) + sum(len(m) for m in msgs)
    print("语料：单条 %d 字符 + %d 条消息（共 %d 字符）"
          % (len(single), len(msgs), total_chars))

    # ---------- 1. 等价性 ----------
    print("\n[1] 数值等价性（差异必须为 0）")
    samples = [single[:n] for n in (0, 1, 7, 100, 9999)] + msgs[:60] + [single]
    diff_fast = [i for i, s in enumerate(samples) if py_old(s) != py_fast(s)]
    print("  py_old vs py_fast : %d / %d 条不一致" % (len(diff_fast), len(samples)))
    print("  py_mem vs py_fast : %s" % ("一致" if all(py_mem(s) == py_fast(s) for s in samples)
                                          else "不一致（两份实现已分叉，需同步）"))

    # ---------- 2. Node worker（IPC 模式与 server.py 一致） ----------
    proc = None
    try:
        proc = subprocess.Popen(["node", "-e", NODE_JS], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", bufsize=1)
        warm = json.dumps({"text": "预热"}, ensure_ascii=False) + "\n"
        proc.stdin.write(warm); proc.stdin.flush()
        proc.stdout.readline()
        node_ok = True
    except Exception as e:
        node_ok = False
        print("\n[!] Node 不可用：%s" % e)

    node_diffs, node_ms, node_calc_ms = 0, 0.0, 0.0
    if node_ok:
        edge = [" ", "\t\n\r", "\xa0", "\u200b", "\ufeff", "a\xa0b", "中 文 123"]
        edge_node = []
        for e in edge:
            proc.stdin.write(json.dumps({"text": e}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            edge_node.append(json.loads(proc.stdout.readline()).get("n"))
        print("  边界样本 py_old : %s" % [py_old(e) for e in edge])
        print("  边界样本 py_fast: %s" % [py_fast(e) for e in edge])
        print("  边界样本 node   : %s" % edge_node)
        # 逐条（最贴近真实调用形态）
        t = time.perf_counter()
        for s in msgs:
            proc.stdin.write(json.dumps({"text": s}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            r = json.loads(proc.stdout.readline())
            if r.get("n") != py_old(s):
                node_diffs += 1
        node_ms = (time.perf_counter() - t) * 1000
        # 纯计算（一次性把全部文本发过去，扣掉往返后 V8 自己花的时间）
        t = time.perf_counter()
        proc.stdin.write(json.dumps({"text": "".join(msgs)}, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        node_calc_ms = (time.perf_counter() - t) * 1000

    # ---------- 3. 性能 ----------
    print("\n[2] 性能对照（ms，3 次取最优）")
    old_single = timeit(py_old, single)
    fast_single = timeit(py_fast, single)
    old_msgs = timeit(lambda ms: [py_old(m) for m in ms], msgs)
    fast_msgs = timeit(lambda ms: [py_fast(m) for m in ms], msgs)

    rows = [
        ("单条 200k 字符", "py_old", old_single),
        ("单条 200k 字符", "py_fast", fast_single),
        ("一轮 %d 条消息" % len(msgs), "py_old", old_msgs),
        ("一轮 %d 条消息" % len(msgs), "py_fast", fast_msgs),
    ]
    if node_ok:
        rows.append(("一轮 %d 条消息" % len(msgs), "node_ipc(含往返)", node_ms))
        rows.append(("一轮 %d 条消息" % len(msgs), "node_ipc(纯计算)", node_calc_ms))
    for scope, impl, ms in rows:
        print("  %-18s %-16s %9.2f ms" % (scope, impl, ms))

    print("\n[3] 结论数据")
    print("  py_fast 加速比(单条) : %.2fx" % (old_single / fast_single if fast_single else 0))
    print("  py_fast 加速比(一轮) : %.2fx" % (old_msgs / fast_msgs if fast_msgs else 0))
    if node_ok:
        print("  node 数值不一致条数  : %d / %d" % (node_diffs, len(msgs)))
        print("  node(含IPC)/py_fast  : %.2fx" % (node_ms / fast_msgs if fast_msgs else 0))
    else:
        print("  node 数值不一致条数  : N/A（Node 不可用）")

    if proc:
        try:
            proc.stdin.close(); proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
