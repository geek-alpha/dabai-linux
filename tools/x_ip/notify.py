"""企业微信群机器人推送（webhook 直发，零认证、零额度、单向）。

开通路径（约 1 分钟，不需要企业认证）：
1. 手机/电脑企业微信 → 进入任意一个群（没有就建一个，把自己拉进去）
2. 群右上角 ... → 群机器人 → 添加机器人 → 起个名（如"草稿推送"）
3. 复制 Webhook URL，填到 x_credentials.json 的 wecom_webhook 字段
   （或设环境变量 WECOM_WEBHOOK，优先级更高）
4. 验证：venv/bin/python -m tools.x_ip.cli notify --test "你好"

限制（官方）：
- 只能群机器人主动推送到群，单向；群里回复机器人收不到。
- 文本消息 content ≤ 2048 字节；每个机器人每分钟 20 条。
"""

from __future__ import annotations

import json
import os
import urllib.request

API = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

CRED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_credentials.json")


def webhook_url() -> str:
    """webhook 地址：环境变量优先，其次 x_credentials.json 的 wecom_webhook。"""
    env = os.environ.get("WECOM_WEBHOOK", "").strip()
    if env:
        return env
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            return (json.load(f).get("wecom_webhook") or "").strip()
    except Exception:
        return ""


def clip_utf8(text: str, limit: int = 2048) -> str:
    """按 UTF-8 字节数截断，保证不把多字节字符劈成两半。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    while text and len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text


def send_text(text: str, timeout: float = 10.0) -> dict:
    """推送一条文本消息。返回 {"ok": bool, "error": ...}。"""
    url = webhook_url()
    if not url:
        return {"ok": False, "error": "未配置 wecom_webhook：企业微信群机器人 Webhook URL 未填"}
    body = json.dumps(
        {"msgtype": "text", "text": {"content": clip_utf8(text)}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        ok = resp.get("errcode") == 0
        return {"ok": ok, "error": resp if not ok else ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
