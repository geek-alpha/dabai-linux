r"""翻墙与代理技能 —— 操作 fq 多协议翻墙启动器，并验证本地代理可用性。

fq CLI 目录由环境变量 DABAI_FQ_ROOT 指定（Windows 默认 D:\AI\Chrome141_AllNew_2025.10.3），支持
clash/xray/hysteria/hysteria2/singbox/naive/juicity/mieru/shadowquic 九种核心。
本技能把常用操作封装成两个工具：
- fq_ctl      启动/停止/切换协议、换 IP、测连通、开关系统代理
- proxy_test  真实走一次请求探测本地代理端口（拿出口 IP）
完整操作手册见同目录 SKILL.md（skill_help("fq-proxy") 可拉取）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess


def _find_fq_root() -> str:
    """定位 fq 启动器目录（跨平台）：DABAI_FQ_ROOT → 平台默认位置。"""
    env = (os.environ.get("DABAI_FQ_ROOT") or "").strip().strip('"')
    cands = [env] if env else []
    if os.name == "nt":
        cands += [r"D:\AI\Chrome141_AllNew_2025.10.3"]
    else:
        cands += ["/opt/fq", "/usr/local/fq", "/usr/share/fq",
                  os.path.expanduser("~/fq"), os.path.expanduser("~/.local/share/fq")]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return env or (r"D:\AI\Chrome141_AllNew_2025.10.3" if os.name == "nt" else "")


FQ_ROOT = _find_fq_root()


def _fq_argv(args: list) -> list:
    """按平台组装 fq 调用命令：Windows 走 fq.cmd，POSIX 走 PATH 里的 fq（或目录内 fq.sh）。"""
    if os.name == "nt":
        return ["cmd", "/c", "fq.cmd"] + args
    exe = shutil.which("fq")
    if exe:
        return [exe] + args
    for name in ("fq.sh", "fq"):
        p = os.path.join(FQ_ROOT, name) if FQ_ROOT else ""
        if p and os.path.isfile(p):
            return [p] + args
    return []

# 协议别名 -> 本地代理地址（与 fq.ps1 顶部定义一致；编号 1-9 在 _resolve_proxy 里映射）
PROTOCOL_PROXIES = {
    "clash": "http://127.0.0.1:7890",
    "clash.meta": "http://127.0.0.1:7890",
    "meta": "http://127.0.0.1:7890",
    "xray": "socks5h://127.0.0.1:1080",
    "hysteria": "socks5h://127.0.0.1:1080",
    "hy": "socks5h://127.0.0.1:1080",
    "hy1": "socks5h://127.0.0.1:1080",
    "singbox": "socks5h://127.0.0.1:1080",
    "sing-box": "socks5h://127.0.0.1:1080",
    "sb": "socks5h://127.0.0.1:1080",
    "naive": "socks5h://127.0.0.1:1080",
    "naiveproxy": "socks5h://127.0.0.1:1080",
    "hysteria2": "socks5h://127.0.0.1:1080",
    "hy2": "socks5h://127.0.0.1:1080",
    "juicity": "socks5h://127.0.0.1:1080",
    "mieru": "socks5h://127.0.0.1:3080",
    "shadowquic": "socks5h://127.0.0.1:4080",
    "sq": "socks5h://127.0.0.1:4080",
    # 本机常驻的另一套代理源
    "sidecar": "http://127.0.0.1:31181",
    "dev-sidecar": "http://127.0.0.1:31181",
}
PROTOCOL_ORDER = ["clash", "xray", "hysteria", "singbox", "naive",
                  "hysteria2", "juicity", "mieru", "shadowquic"]

# 各子命令的执行超时（秒）；agent 单工具上限约 120s，留出余量
_ACTION_TIMEOUT = {
    "list": 30, "status": 30, "start": 110, "stop": 60,
    "test": 110, "update": 115, "chrome": 45, "sysproxy": 30,
}

DEV_SIDECAR_RESTORE_NOTE = (
    "\n⚠ 系统代理已被改动。本机系统代理平时由 dev-sidecar 托管"
    "（ProxyServer=https=http://127.0.0.1:31181, ProxyEnable=1），"
    "用完 sysproxy 后必须恢复原状并广播 WinINET 刷新；"
    "若开关被自动关掉是 dev-sidecar 的自我保护，重设一次即可。"
)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clip(text: str, limit: int = 3500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:2200] + "\n…（中间省略）…\n" + text[-(limit - 2300):]


# ---------- 工具 1：fq_ctl ----------

def fq_ctl(args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTION_TIMEOUT:
        return (f"不支持的操作 '{action}'。可用：list/status/start/stop/test/update/chrome/sysproxy。"
                "详细用法见 skill_help(\"fq-proxy\")。")

    target = str(args.get("target") or "").strip()
    cmd_args = [action]

    if action == "sysproxy":
        if target not in ("on", "off", "status"):
            return "sysproxy 需要 target=on/off/status。"
        cmd_args.append(target)
    elif action in ("start", "update"):
        if not target:
            return f"{action} 必须指定协议（如 clash/xray/hy2 或编号 1-9）。"
        if args.get("ip_source") is not None:
            cmd_args += ["-Ip", str(int(args["ip_source"]))]
        if action == "update" and not args.get("confirm_update"):
            return ("update 会从云端下载新配置并覆盖现有配置文件（旧配置备份为 *_backup）。"
                    "请确认用户明确要求换 IP 后，带 confirm_update=true 重试。")
        if action == "start":
            if args.get("no_chrome", True):
                cmd_args.append("-NoChrome")
            if args.get("no_elevate", True):
                cmd_args.append("-NoElevate")
    else:
        # status/test/stop/chrome：目标可选（all 或单个协议）
        if target:
            cmd_args.append(target)
        if action == "stop" and args.get("keep_chrome"):
            cmd_args.append("-KeepChrome")

    argv = _fq_argv(cmd_args)
    if not argv or not FQ_ROOT:
        return ("fq 启动器不可用：未找到 fq 命令或目录。Windows 默认 "
                r"D:\AI\Chrome141_AllNew_2025.10.3；Linux/macOS 请设 DABAI_FQ_ROOT "
                "指向 fq 所在目录，或把 fq 放进 PATH。")
    try:
        proc = subprocess.run(
            argv, cwd=FQ_ROOT, capture_output=True, timeout=_ACTION_TIMEOUT[action],
        )
    except subprocess.TimeoutExpired:
        return (f"fq {action} 超时（>{_ACTION_TIMEOUT[action]}s）。"
                "可能卡在 UAC 提权弹窗——用 no_elevate=true 重试，或让用户手动确认弹窗。")
    except Exception as e:
        return f"fq {action} 启动失败：{e}"

    out = _decode(proc.stdout or b"")
    err = _decode(proc.stderr or b"")
    body = _clip((out + ("\n[stderr]\n" + err if err.strip() else "")).strip() or "（无输出）")
    tail = f"\n[exit={proc.returncode}]"
    if action == "sysproxy" and target == "on" and proc.returncode == 0:
        tail += DEV_SIDECAR_RESTORE_NOTE
    if action == "start" and not args.get("no_elevate", True):
        tail += "\n提示：本次允许了 UAC 提权，若命令像挂住一样无输出，多半是在等用户点弹窗。"
    return body + tail


# ---------- 工具 2：proxy_test ----------

def _resolve_proxy(value: str) -> str:
    v = value.strip().lower()
    return PROTOCOL_PROXIES.get(v, value.strip())


def proxy_test(args: dict) -> str:
    raw = str(args.get("proxy") or "").strip()
    url = str(args.get("url") or "https://httpbin.org/ip").strip()

    if raw:
        candidates = [_resolve_proxy(raw)]
    else:
        # 去重保序：clash 的 http 端口放最前，dev-sidecar 兜底
        seen, candidates = set(), []
        for p in [PROTOCOL_PROXIES[k] for k in PROTOCOL_ORDER] + [PROTOCOL_PROXIES["sidecar"]]:
            if p not in seen:
                seen.add(p)
                candidates.append(p)

    lines = []
    for proxy in candidates:
        try:
            proc = subprocess.run(
                ["curl", "-sS", "--ssl-no-revoke", "-m", "8", "-x", proxy, url],
                capture_output=True, timeout=15,
            )
            out = _decode(proc.stdout or b"").strip()
            err = _decode(proc.stderr or b"").strip()
            if proc.returncode == 0 and out:
                m = (re.search(r'"origin"\s*:\s*"([^"]+)"', out)
                     or re.search(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', out))
                ip = m.group(1) if m and m.lastindex else (m.group(0) if m else "")
                lines.append(f"✅ {proxy} → 出口 {ip or '(无法解析，响应原文见下)'}  {out[:120]}")
            else:
                reason = (err.splitlines() or ["失败"])[-1][:120]
                lines.append(f"❌ {proxy} → 不可用（{reason}）")
        except subprocess.TimeoutExpired:
            lines.append(f"❌ {proxy} → 超时（端口在监听但通道不通，IP 可能已被墙）")
        except Exception as e:
            lines.append(f"❌ {proxy} → 测试出错：{e}")

    header = f"测试目标：{url}\n" if raw == "" else ""
    result = header + "\n".join(lines)
    if raw == "":
        result += ("\n说明：全部不可用时先 `fq_ctl(action=\"start\", target=\"clash\")` 起一个；"
                   "端口监听但超时多为远端 IP 被墙，用 ip_source 换 IP 或换协议。")
    return _clip(result)


HANDLERS = {
    "fq_ctl": fq_ctl,
    "proxy_test": proxy_test,
}
