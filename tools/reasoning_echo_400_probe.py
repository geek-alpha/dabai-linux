#!/usr/bin/env python3
"""探针 v3：找出 400「reasoning_content must be passed back」的真实触发条件。

v1/v2 结论（都已实测）：
  - 手写 assistant(tool_calls) 缺 reasoning_content            → 200
  - 真实工具轮 assistant 删掉 reasoning（非流式/流式、带不带 tools）→ 200
  - 补 reasoning="" 或占位文本                                  → 200
即：**单条缺 reasoning 不触发校验**，v2 的复现是假阴性。

剩下最像真的假设：**混合状态** —— 请求里有的 assistant 带 reasoning_content、
有的不带，中转校验（newapi 风格）判「你既然支持回传，就必须全回传」。
真实 agent.py 恰好是混合的：工具轮的 assistant 由 4785/4811 挂上 reasoning，
而历史注入(3837/3843)与孤立 tool 修复(825)产出的 assistant 永远不带。

本探针真跑两轮工具调用拿到两条真实 assistant，再对「谁带谁不带」做 2x2 对照：
  4a 第一条删、第二条带   ← 假设的复现点
  4b 都带（基线）
  4c 都删
  4d 4a + 给缺的那条补 reasoning_content=""
  4e 4a 流式
另附 5：三条 assistant 中只有中间一条带（进一步确认「混合」而非「位置」）

用法：/home/wxf/dabai/venv/bin/python tools/reasoning_echo_400_probe.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "/home/wxf/dabai")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openai import AsyncOpenAI  # noqa: E402

CFG = json.load(open("/home/wxf/dabai/settings.json", encoding="utf-8"))
MODEL = CFG.get("model")
THINKING = {"reasoning_effort": "high", "thinking_budget": 2000}

client = AsyncOpenAI(base_url=CFG["base_url"], api_key=CFG["api_key"])

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_lines",
        "description": "读取文件指定行区间",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "start": {"type": "integer"}},
            "required": ["path"],
        },
    },
}]


async def one_turn(messages, label):
    r = await client.chat.completions.create(
        model=MODEL, messages=messages, tools=TOOLS,
        max_tokens=200, temperature=0.1, extra_body=THINKING)
    m = r.choices[0].message
    rc = getattr(m, "reasoning_content", None) or ""
    tc = getattr(m, "tool_calls", None)
    print(f"  · {label}: tool_calls={'有' if tc else '无'} reasoning_len={len(rc)}")
    if not tc:
        return None, ""
    a = {"role": "assistant", "content": m.content or "",
         "tool_calls": [t.model_dump() for t in tc]}
    return a, rc


def with_r(a, rc):
    m = dict(a)
    m["reasoning_content"] = rc
    return m


async def call(label, messages, stream=False, tools=True):
    kw = dict(model=MODEL, messages=messages, max_tokens=64, temperature=0.1,
              extra_body=THINKING)
    if tools:
        kw["tools"] = TOOLS
    try:
        if stream:
            s = await client.chat.completions.create(stream=True, **kw)
            n = 0
            async for _c in s:
                n += 1
            print(f"[{label}] 200 OK（流式 {n} chunk）")
        else:
            r = await client.chat.completions.create(**kw)
            print(f"[{label}] 200 OK | {(r.choices[0].message.content or '')[:34]!r}")
        return True
    except Exception as e:
        body = str(e)
        print(f"[{label}] ERR | 命中must-be-passed-back={'must be passed back' in body}"
              f" | {body[:220]}")
        return False


async def main():
    print(f"渠道={CFG['base_url']} 模型={MODEL}\n阶段1：真跑两轮工具调用，收集真实 assistant")
    m0 = [{"role": "user", "content": "读 /tmp/probe.txt 的第 1 行，只调工具别回答。"}]
    a1, r1 = await one_turn(m0, "轮1")
    if not a1:
        print("⚠ 轮1 未走工具轮，退出"); return
    tid1 = a1["tool_calls"][0]["id"]

    m1 = m0 + [with_r(a1, r1),
               {"role": "tool", "tool_call_id": tid1, "content": "第1行：hello probe"},
               {"role": "user", "content": "再读 /tmp/probe.txt 的第 2 行，只调工具别回答。"}]
    a2, r2 = await one_turn(m1, "轮2")
    if not a2:
        print("⚠ 轮2 未走工具轮，退出"); return
    tid2 = a2["tool_calls"][0]["id"]
    tail = [{"role": "tool", "tool_call_id": tid2, "content": "第2行：second line"},
            {"role": "user", "content": "两行都读到了，一句话总结。"}]

    def build(x1, x2):
        return m0 + [x1, {"role": "tool", "tool_call_id": tid1, "content": "第1行：hello probe"},
                     {"role": "user", "content": "再读第 2 行，只调工具别回答。"}] + [x2] + tail

    print("\n阶段2：谁带谁不带")
    await call("4a 第一条删+第二条带 ← 假设复现点", build(a1, with_r(a2, r2)))
    await call("4b 都带（基线）", build(with_r(a1, r1), with_r(a2, r2)))
    await call("4c 都删", build(a1, a2))
    await call("4d 4a + 缺的补空串", build({**a1, "reasoning_content": ""}, with_r(a2, r2)))
    await call("4e 4a 流式", build(a1, with_r(a2, r2)), stream=True)
    await call("5 只有第二条带（单条混合）",
               m0 + [with_r(a1, r1),
                     {"role": "tool", "tool_call_id": tid1, "content": "第1行：hello probe"},
                     {"role": "user", "content": "再读第 2 行，只调工具别回答。"},
                     a2, *tail])


if __name__ == "__main__":
    asyncio.run(main())
