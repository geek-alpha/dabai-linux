#!/usr/bin/env python3
"""assistant reasoning_content 回传的 token 白付开销实测。

背景：agent.py 4785/4809 把 round_reasoning 作为 reasoning_content 回传给
thinking 系渠道（DeepSeek 要求原样回传，丢了整轮 400）；但 pending_chars
（agent.py 4780/4802）只统计 content + tool_calls，没算 reasoning 字符数。

本探针在同一进程、同一前缀下做受控 A/B：
  A 组 = assistant 消息不带 reasoning_content
  B 组 = 同一条消息带上长度为 N 的 reasoning_content
  Δ = B.prompt_tokens - A.prompt_tokens → 回传 reasoning 的白付 token
再用多个 N 拟合「字符 → token」比（中文/英文各一组）。

用法：
  venv/bin/python tools/reasoning_echo_probe.py
"""
import asyncio
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openai import AsyncOpenAI  # noqa: E402

SYS = "你是一个测试用助手，只做最小回复。"
USER = "只回复两个字：收到"
REPLY = "收到"

ZH = ("先看目标：这一轮只推一个动作，做完就停。"
      "接着拆解约束：不可逆动作不做，卡住也要落盘接力棒。"
      "然后验证证据：命令原文、退出码、文件行号三者齐备才算完成。")
EN = ("First pin the goal: this round advances exactly one action, then stop. "
      "Next split the constraints: no irreversible moves, and the baton must be "
      "written even when blocked. Then check evidence: command, exit code, and "
      "file line numbers together count as done. ")


def rep(sample: str, n: int) -> str:
    if n <= 0:
        return ""
    out = (sample * (n // len(sample) + 1))[:n]
    return out


def _model_ok(want: str, got: str) -> bool:
    """渠道会静默降级路由（请求 v4-pro，实测回来 flash）。降级路由对
    reasoning_content 基本不计费，A/B 会得到恒定假 Δ——必须当场判无效。"""
    return bool(got) and got.strip().lower() == want.strip().lower()


async def ask(client, model: str, reasoning, content: str = REPLY) -> dict:
    """发一次请求，返回 prompt_tokens / 实际路由 model / 报错信息。"""
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYS},
                      {"role": "user", "content": USER},
                      msg],
            max_tokens=1,
        )
        got = str(getattr(r, "model", "") or "")
        if not _model_ok(model, got):
            # 不重试：降级是渠道侧决策，重试只会再拿一个假样本
            return {"p": None, "got": got,
                    "err": f"ROUTE_DRIFT: 请求 {model}，实际 {got}"}
        return {"p": int(r.usage.prompt_tokens), "err": None, "got": got}
    except Exception as e:  # noqa: BLE001
        return {"p": None, "got": "", "err": f"{type(e).__name__}: {e}"}


async def ask_retry(client, model: str, reasoning, tries: int = 4) -> dict:
    """渠道偶发 5xx / 限流时重试：本渠道 rpm 很紧，退避要给够。"""
    last = {"p": None, "err": "no attempt"}
    for i in range(tries):
        last = await ask(client, model, reasoning)
        if not last["err"]:
            return last
        if str(last["err"]).startswith("ROUTE_DRIFT"):
            return last
        await asyncio.sleep(15.0 * (i + 1))
    return last


async def main() -> int:
    cfg = json.load(open(os.path.join(BASE, "settings.json"), encoding="utf-8"))
    model = str(cfg.get("model") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not (model and base_url and api_key):
        print("✗ settings.json 缺 model/base_url/api_key")
        return 2
    print(f"模型 {model} @ {base_url}")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    a = await ask_retry(client, model, None)
    if a["err"]:
        _dump_invalid(model, a)
        print(f"✗ 基准请求失败/路由降级：{a['err']}")
        return 3 if str(a["err"]).startswith("ROUTE_DRIFT") else 2
    base = a["p"]
    print(f"A 组（不回传）prompt_tokens = {base}，实际路由 {a['got']}")

    await asyncio.sleep(20.0)
    empty = await ask_retry(client, model, "")
    if empty["err"]:
        print(f"✗ 空 reasoning_content 被拒：{empty['err']}")
        return 1
    print(f"空串 reasoning_content = {empty['p']}（字段存在本身开销 "
          f"{empty['p'] - base:+d}）")

    rows = []
    for label, sample in (("中文", ZH), ("英文", EN)):
        for n in (200, 1000, 4000):
            await asyncio.sleep(20.0)
            r1 = await ask_retry(client, model, rep(sample, n), tries=3)
            if r1["err"]:
                print(f"✗ {label} N={n} 失败：{r1['err']}")
                return 1
            d1 = r1["p"] - base
            ratio = d1 / n
            rows.append({"lang": label, "chars": n, "delta": d1,
                         "tok_per_char": round(ratio, 4)})
            print(f"{label} N={n:>6} 字符 → Δ={d1:>6} token（{ratio:.4f} token/字符）")

    print("\n—— 拟合 ——")
    for label in ("中文", "英文"):
        pts = [r for r in rows if r["lang"] == label]
        k = sum(r["delta"] for r in pts) / sum(r["chars"] for r in pts)
        lin = (pts[-1]["delta"] - pts[0]["delta"]) / (pts[-1]["chars"] - pts[0]["chars"])
        print(f"{label}：整体均值 {k:.4f} token/字符，长样本斜率 {lin:.4f}；"
              f"按 1000 字符推理 ≈ {k * 1000:.0f} token")

    out = os.path.join(BASE, "data", "reasoning_echo_probe.json")
    json.dump({"valid": True, "model": model, "route": a["got"],
               "base_prompt_tokens": base, "empty_field": empty["p"],
               "rows": rows}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n结果落盘：{out}")
    return 0


def _dump_invalid(model: str, res: dict) -> None:
    """路由降级 = 本次 A/B 无效，落盘标记，别让假数据混进结论。"""
    out = os.path.join(BASE, "data", "reasoning_echo_probe.json")
    json.dump({"valid": False, "model": model, "route": res.get("got") or "",
               "error": res.get("err"), "rows": []},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"结果落盘（valid=false）：{out}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
