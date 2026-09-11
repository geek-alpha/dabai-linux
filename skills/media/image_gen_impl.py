"""AI 画图实现 —— 调用 settings.json 中配置的绘图模型生成图片。

默认端点：SiliconFlow 的 OpenAI 兼容接口（Kwai-Kolors/Kolors）。
配置在 settings.json：images_base_url / images_model / images_api_key。
图片保存到项目 web/generated/ 目录，返回 /generated/<文件名> 访问链接
（server.py 已把该目录挂载为静态资源）。

并入自原 image_gen 技能。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("image_gen")

import requests

BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUT_DIR = BASE_DIR / "web" / "generated"
SETTINGS = BASE_DIR / "settings.json"

_DEFAULT_BASE = "https://api.siliconflow.cn/v1"
_DEFAULT_MODEL = "Kwai-Kolors/Kolors"
_TIMEOUT = 120  # 绘图通常需要几十秒


def _load_images_config() -> dict:
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "base_url": str(cfg.get("images_base_url") or _DEFAULT_BASE).rstrip("/"),
            "model": str(cfg.get("images_model") or _DEFAULT_MODEL),
            "api_key": str(cfg.get("images_api_key") or ""),
        }
    except Exception:
        return {"base_url": _DEFAULT_BASE, "model": _DEFAULT_MODEL, "api_key": ""}


def _cloud_proxy() -> dict:
    """按 settings.json 的 llm_proxy 解析画图请求的 proxies（auto 自动拉起 fq 代理）。"""
    try:
        from proxy_utils import is_local_url, requests_proxies, resolve_llm_proxy
        with open(SETTINGS, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base = str(cfg.get("images_base_url") or _DEFAULT_BASE)
        if is_local_url(base):
            return {}
        return requests_proxies(resolve_llm_proxy(cfg))
    except Exception:
        return {}


def _generate_blocking(prompt: str, size: str, cfg: dict) -> str:
    """同步执行的生成 + 下载（在线程里跑）。返回本地访问链接。"""
    if not cfg["api_key"]:
        return "未配置绘图 API Key：请在 settings.json 填写 images_api_key（默认用 SiliconFlow）。"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"}
    body = {
        "model": cfg["model"],
        "prompt": prompt,
        "image_size": size,
        "batch_size": 1,
    }
    resp = requests.post(cfg["base_url"] + "/images/generations",
                         json=body, headers=headers, timeout=_TIMEOUT,
                         proxies=_cloud_proxy())
    if resp.status_code != 200:
        return f"绘图接口返回 {resp.status_code}：{resp.text[:200]}"
    data = resp.json()
    # 兼容两种响应：OpenAI 风格 data[0].url / SiliconFlow 风格 images[0].url
    url = None
    for key in ("images", "data"):
        arr = data.get(key) or []
        if isinstance(arr, list) and arr:
            item = arr[0] or {}
            url = item.get("url") or item.get("b64_json")
            if url:
                break
    if not url:
        return "绘图接口响应里没有找到图片：" + str(data)[:200]
    name = "img_%d.png" % int(time.time() * 1000)
    dest = OUT_DIR / name
    try:
        if url.startswith("data:") or url.startswith("http"):
            if url.startswith("data:"):
                b64 = url.split(",", 1)[1]
                dest.write_bytes(base64.b64decode(b64))
            else:
                r = requests.get(url, timeout=60, proxies=_cloud_proxy())
                r.raise_for_status()
                dest.write_bytes(r.content)
        else:
            dest.write_bytes(base64.b64decode(url))
    except Exception as e:
        return f"图片保存失败：{e}"
    size_mb = dest.stat().st_size / (1024 * 1024)
    link = f"/generated/{name}"
    _register_task(link, prompt)
    return f"画好啦！图片已保存（{size_mb:.1f}MB）：打开链接查看 -> {link}"


def _register_task(link: str, prompt: str) -> None:
    """把生成的图片登记成一条任务并写入 result，让大屏(/api/tasks 轮询)能拉到。

    大屏只从任务 result 里抠图片链接，所以画完图必须走任务系统，
    否则图片永远只出现在聊天框、上不了大屏。
    """
    try:
        from task_orchestrator import get_orchestrator
        orch = get_orchestrator()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_create_and_finish(orch, link, prompt))
        else:
            asyncio.run(_create_and_finish(orch, link, prompt))
    except Exception as e:
        logger.warning("登记画图任务失败（不影响图片本身）: %s", e)


async def _create_and_finish(orch, link: str, prompt: str) -> None:
    """创建任务并收尾（add_step/set_result 都是 orchestrator 的方法，不是 Task 的）。"""
    try:
        task = await orch.create(
            kind="image",
            title="AI 画图：" + (prompt[:60] if prompt else "图片"),
            ws=None,
            brief=prompt,
            confirm=False,
            channel="image",
        )
        await orch.add_step(task, "图片已生成")
        await orch.set_result(task, link, status="done")
    except Exception as e:
        logger.warning("画图任务登记/收尾失败: %s", e)


async def create_image(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "缺少画面描述（prompt 参数不能为空）。"
    size = str(args.get("size") or "1024x1024").strip()
    if size not in ("1024x1024", "768x1024", "1024x768"):
        size = "1024x1024"
    cfg = _load_images_config()
    try:
        return await asyncio.to_thread(_generate_blocking, prompt, size, cfg)
    except Exception as e:
        return f"画图失败：{e.__class__.__name__}: {e}"