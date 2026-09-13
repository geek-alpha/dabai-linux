#!/usr/bin/env python3
"""图片注入 token 增量实测。

核对 agent._IMG_TOKEN_EST（400）的估算是否对得上提供方真实计费：
同一条文本请求，发两次 —— 不带图 / 带一张经生产编码路径处理的图，
两次 usage.prompt_tokens 的差值就是这张图实际吃掉的 token。

用法：venv/bin/python tools/img_token_probe.py [图片路径]
不带参数时自造一张 720x1280 测试图（模拟手机截图）。
"""
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from openai import AsyncOpenAI  # noqa: E402

from agent import _IMG_MAX_SIDE, _IMG_TOKEN_EST, _img_data_url  # noqa: E402

PROMPT = "只回复两个字：收到"


def make_shot(path: str) -> None:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (720, 1280), (22, 24, 30))
    d = ImageDraw.Draw(im)
    for i in range(14):
        y = 90 + i * 80
        d.rectangle([40, y, 680, y + 56], fill=(48, 52, 64))
        d.text((64, y + 20), f"row {i}  tab-bar  item-{i}", fill=(210, 214, 222))
    d.rectangle([0, 1180, 720, 1280], fill=(38, 40, 50))
    im.save(path)


async def ask(client, model: str, content) -> int:
    r = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=8,
    )
    return int(r.usage.prompt_tokens)


async def main() -> int:
    cfg = json.load(open(os.path.join(BASE, "settings.json"), encoding="utf-8"))
    model = str(cfg.get("model") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not (model and base_url and api_key):
        print("✗ settings.json 缺 model/base_url/api_key")
        return 2

    shot = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "data", "img_probe_shot.png")
    if not os.path.isfile(shot):
        os.makedirs(os.path.dirname(shot), exist_ok=True)
        make_shot(shot)

    url = _img_data_url(shot)
    if not url:
        print("✗ 图片编码失败（生产同路径 _img_data_url 返回空）")
        return 2
    from PIL import Image
    w, h = Image.open(shot).size
    print(f"模型 {model} @ {base_url}")
    print(f"测试图 {shot} 原始 {w}x{h}，编码后 base64 {len(url) / 1024:.1f} KB"
          f"（长边上限 {_IMG_MAX_SIDE}）")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    p_text = await ask(client, model, PROMPT)
    p_img = await ask(client, model, [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": url}},
    ])
    delta = p_img - p_text
    print(f"纯文本 prompt_tokens = {p_text}")
    print(f"带图   prompt_tokens = {p_img}")
    print(f"→ 单图实际增量 = {delta} token")
    print(f"→ 代码估算 _IMG_TOKEN_EST = {_IMG_TOKEN_EST}"
          f"（偏差 {_IMG_TOKEN_EST - delta:+d}）")
    ok = abs(delta - 384) <= 384 * 0.35
    print(f"→ 与「≈384」核对：{'一致' if ok else '不一致'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
