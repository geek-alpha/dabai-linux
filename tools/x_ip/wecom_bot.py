"""企业微信智能机器人 · 长连接客户端（官方 API 模式，无公网 IP 依赖）。

协议（developer.work.weixin.qq.com/document/path/101463）：
- 连接  wss://openws.work.weixin.qq.com
- 订阅  {"cmd":"aibot_subscribe","headers":{"req_id":..},"body":{"bot_id":..,"secret":..}}
- 心跳  {"cmd":"ping","headers":{"req_id":..}} 每 30s
- 推送  {"cmd":"aibot_send_msg","headers":{"req_id":..},"body":{"chatid":..,"chat_type":1,"msgtype":"text|markdown",...}}
- 收消息 aibot_msg_callback / aibot_event_callback（disconnected_event 等）

限制：同一 bot 同时只允许一个长连接，新连接踢旧连接；主动推送前需用户先给机器人发过消息；
      单会话 30 条/分钟、1000 条/小时。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import uuid


# ---------- 主动推送队列（serve 常驻消费，cron 只写文件，不踢长连接） ----------

PUSH_QUEUE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_queue")


def queue_push(chatid: str, content: str, msgtype: str = "markdown") -> str:
    """把一条推送写成队列文件，serve 会在 30 秒内用长连接发出。返回文件名。"""
    os.makedirs(PUSH_QUEUE_DIR, exist_ok=True)
    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    path = os.path.join(PUSH_QUEUE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"chatid": chatid, "msgtype": msgtype, "content": content}, f, ensure_ascii=False)
    return path


async def _drain_push_queue(ws) -> int:
    """扫描推送队列，用当前长连接逐条发出。返回成功条数。"""
    if not os.path.isdir(PUSH_QUEUE_DIR):
        return 0
    ok_n = 0
    for name in sorted(os.listdir(PUSH_QUEUE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(PUSH_QUEUE_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                item = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[push_queue] 坏文件 {name}: {e}，跳过")
            continue
        try:
            resp = await send_msg(ws, item["chatid"], item["content"], item.get("msgtype", "markdown"))
        except Exception as e:
            print(f"[push_queue] 发送 {name} 异常: {e}")
            continue
        err = (resp.get("body") or {}).get("errcode", resp.get("errcode", -1))
        with open(path + ".done", "w", encoding="utf-8") as f:
            json.dump({"ok": err in (0, None), "errcode": err, "at": int(time.time())}, f)
        if err in (0, None):
            ok_n += 1
            print(f"[push_queue] ✓ 已推送 {name} → {item['chatid']}")
        else:
            print(f"[push_queue] ✗ {name} errcode={err}: {json.dumps(resp, ensure_ascii=False)[:200]}")
        try:
            os.remove(path)
        except OSError:
            pass
    return ok_n


# ---------- response_url 落盘 ----------

def save_response(msg_body: dict) -> None:
    """把回调里的 response_url + 用户标识落盘，供 push 复用。"""
    url = msg_body.get("response_url") or ""
    if not url:
        return
    frm = msg_body.get("from") or {}
    data = {
        "response_url": url,
        "userid": frm.get("userid", ""),
        "updated_at": int(time.time()),
    }
    with open(LATEST_RESPONSE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(LATEST_RESPONSE_FILE, 0o600)


def load_response() -> dict:
    if not os.path.exists(LATEST_RESPONSE_FILE):
        return {}
    try:
        with open(LATEST_RESPONSE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def reply_via_response_url(response_url: str, content: str, msgtype: str = "text") -> int:
    """POST 到 response_url 回复（官方被动回复通道，纯 HTTP，不占长连接）。返回 0=成功。"""
    body = {"msgtype": msgtype}
    if msgtype == "markdown":
        body["markdown"] = {"content": content}
    else:
        body["text"] = {"content": content}
    req = urllib.request.Request(
        response_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        r = json.loads(raw)
    except json.JSONDecodeError:
        print(f"✗ 响应非 JSON: {raw[:200]}")
        return 1
    err = r.get("errcode", 0)
    if err in (0, None):
        return 0
    print(f"✗ 回复失败 errcode={err}: {json.dumps(r, ensure_ascii=False)[:400]}")
    return 1

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30.0
CRED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wecom_bot.json")
# 最近一次用户消息的 response_url 落盘，push 通过它回复（官方被动回复通道，不占长连接）
LATEST_RESPONSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wecom_latest_response.json")

# ---------- 凭证 ----------

def load_cred() -> dict:
    if not os.path.exists(CRED_FILE):
        print(f"✗ 未配置凭证：{CRED_FILE}（应含 bot_id + secret）")
        sys.exit(1)
    with open(CRED_FILE, encoding="utf-8") as f:
        cred = json.load(f)
    if not cred.get("bot_id") or not cred.get("secret"):
        print("✗ 凭证不完整：需要 bot_id 和 secret")
        sys.exit(1)
    return cred


def save_cred(bot_id: str, secret: str) -> None:
    data = {"bot_id": bot_id, "secret": secret, "updated_at": int(time.time())}
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(CRED_FILE, 0o600)


def _req_id() -> str:
    return uuid.uuid4().hex


def _dump(resp: dict) -> None:
    print(json.dumps(resp, ensure_ascii=False))


# ---------- 协议原语 ----------

async def _recv_until(ws, want_req_id: str, timeout: float = 8.0) -> dict:
    """读到 headers.req_id 匹配的响应；顺带把不匹配的消息打印出来（回调）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError(f"等待响应超时（req_id={want_req_id}）")
        raw = await asyncio.wait_for(ws.recv(), timeout=left)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[非JSON帧] {raw[:300]}")
            continue
        rid = (msg.get("headers") or {}).get("req_id")
        if rid == want_req_id:
            return msg
        # 别的 req_id：多半是服务端主动回调，原样展示
        print(f"[回调] {json.dumps(msg, ensure_ascii=False)[:800]}")


async def subscribe(ws, bot_id: str, secret: str) -> dict:
    rid = _req_id()
    await ws.send(json.dumps({
        "cmd": "aibot_subscribe",
        "headers": {"req_id": rid},
        "body": {"bot_id": bot_id, "secret": secret},
    }))
    return await _recv_until(ws, rid)


async def send_msg(ws, chatid: str, content: str, msgtype: str = "text", chat_type: int = 1) -> dict:
    rid = _req_id()
    body = {"chatid": chatid, "chat_type": chat_type, "msgtype": msgtype}
    if msgtype == "markdown":
        body["markdown"] = {"content": content}
    else:
        body["text"] = {"content": content}
    await ws.send(json.dumps({
        "cmd": "aibot_send_msg",
        "headers": {"req_id": rid},
        "body": body,
    }))
    return await _recv_until(ws, rid)


async def heartbeat(ws) -> dict:
    rid = _req_id()
    await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": rid}}))
    return await _recv_until(ws, rid, timeout=15.0)


async def _open_ws():
    if websockets is None:
        print("✗ 缺少 websockets 库：venv/bin/pip install websockets")
        sys.exit(1)
    cred = load_cred()
    ws = await websockets.connect(WS_URL, open_timeout=15, ping_interval=None, max_size=16 * 1024 * 1024)
    try:
        resp = await subscribe(ws, cred["bot_id"], cred["secret"])
        err = (resp.get("body") or {}).get("errcode", 0)
        if err not in (0, None):
            print(f"✗ 订阅失败 errcode={err}: {json.dumps(resp, ensure_ascii=False)[:400]}")
            await ws.close()
            sys.exit(2)
        return ws
    except BaseException:
        await ws.close()
        raise


# ---------- 命令 ----------

def cmd_set(args) -> int:
    save_cred(args.bot_id, args.secret)
    print(f"✓ 已写入凭证 → {CRED_FILE}（0600）")
    return 0


async def cmd_verify() -> int:
    ws = await _open_ws()
    print("✓ 订阅成功，凭证有效：", CRED_FILE)
    _dump(await heartbeat(ws))
    await ws.close()
    return 0


async def cmd_push(chatid: str, content: str, msgtype: str) -> int:
    """通过最近一次回调的 response_url 回复（官方被动回复通道）。

    不占长连接（纯 HTTP），因此不会踢掉 serve；
    前提：用户最近给机器人发过消息（response_url 已落盘）。
    """
    data = load_response()
    url = data.get("response_url", "")
    if not url:
        print(f"✗ 没有可用的 response_url——请先让用户在企业微信里给机器人发一条消息。")
        print(f"  （收到消息后 serve 会把 response_url 存到 {LATEST_RESPONSE_FILE}）")
        return 1
    rc = reply_via_response_url(url, content, msgtype)
    if rc == 0:
        who = data.get("userid") or chatid
        print(f"✓ 已回复 → {who}（{msgtype}）")
    return rc


async def cmd_serve() -> int:
    """常驻：心跳保活 + 断线重连 + 收消息回调（用于建立/确认 chatid）。"""
    cred = load_cred()
    print(f"常驻连接启动 bot_id={cred['bot_id']}，Ctrl+C 退出")
    while True:
        try:
            ws = await _open_ws()
            print(f"[{time.strftime('%H:%M:%S')}] 已连接，开始心跳+收消息")
            async with asyncio.timeout(HEARTBEAT_INTERVAL):
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        print(f"[非JSON帧] {raw[:300]}")
                        continue
                    cmd = msg.get("cmd")
                    print(f"[{time.strftime('%H:%M:%S')}] {cmd}: {json.dumps(msg, ensure_ascii=False)[:800]}")
                    if cmd == "aibot_msg_callback":
                        body = msg.get("body") or {}
                        save_response(body)
                        print(">>> 用户发消息了！response_url 已落盘：", body.get("from", {}).get("userid"))
                    if cmd == "ping" or (cmd is None and (msg.get("headers") or {}).get("req_id") == "ping"):
                        # 服务端也可能要求回 ping 帧，统一回文本 ping 保持活跃
                        await ws.send(json.dumps({"cmd": "ping", "headers": {"req_id": _req_id()}}))
        except (asyncio.TimeoutError, TimeoutError):
            # 30 秒没消息 → 发心跳续命 + 顺带消费推送队列
            try:
                async with asyncio.timeout(5):
                    _dump(await heartbeat(ws))
                await _drain_push_queue(ws)
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] 心跳失败: {e}")
                try:
                    await ws.close()
                except Exception:
                    pass
                continue
        except KeyboardInterrupt:
            print("\n退出")
            try:
                await ws.close()
            except Exception:
                pass
            return 0
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 连接异常: {e}，3 秒后重连")
            try:
                await ws.close()
            except Exception:
                pass
            await asyncio.sleep(3)


def main() -> int:
    p = argparse.ArgumentParser(description="企业微信智能机器人长连接客户端")
    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("verify", help="验证 BotID/Secret 有效性（订阅+心跳）")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("serve", help="常驻长连接：心跳+收消息+自动重连")
    s.set_defaults(fn=cmd_serve)

    pu = sub.add_parser("push", help="主动推送一条消息")
    pu.add_argument("--chatid", required=True, help="会话 ID（单聊=用户 userid，群聊=chatid）")
    pu.add_argument("--text", default="", help="文本内容")
    pu.add_argument("--markdown", default="", help="markdown 内容（与 --text 二选一）")
    pu.set_defaults(fn=cmd_push)

    st = sub.add_parser("set", help="写入凭证（bot_id + secret）")
    st.add_argument("--bot-id", required=True)
    st.add_argument("--secret", required=True)
    st.set_defaults(fn=cmd_set)

    args = p.parse_args()
    if not getattr(args, "fn", None):
        p.print_help()
        return 1

    async def _run():
        if args.cmd == "set":
            return cmd_set(args)
        if args.cmd == "push":
            if not (args.text or args.markdown):
                print("✗ push 需要 --text 或 --markdown")
                return 1
            return await cmd_push(args.chatid, args.markdown or args.text, "markdown" if args.markdown else "text")
        return await args.fn()

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
