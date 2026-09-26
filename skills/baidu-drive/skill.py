# -*- coding: utf-8 -*-
"""百度网盘技能实现 —— 官方 bdpan CLI 的 dabai 适配层。

上游：https://github.com/baidu-netdisk/bdpan-storage （skills/baidu-drive，v1.7.5）
上游是 Claude Code 风格的 SKILL.md + bash 脚本，由 Agent 自己拼 shell 命令；
这里收成 3 个工具（bdpan / bdpan_status / bdpan_login），公共参数在代码里统一注入，
登录固定走上游 scripts/login.sh，裸调 bdpan login 一律拒绝。

上游安全约束（SKILL.md「安全约束」节）在本层的落地：
  1. 登录只走 scripts/login.sh，禁 bdpan login*；
  2. 不读 ~/.config/bdpan/config.json（本模块只透传 CLI stdout，不碰凭据文件）；
  3. update/install/logout 不在本层代劳，需用户明确指令；
  4. 不设置 BDPAN_* 环境变量（只往 PATH 前面补 ~/.local/bin，因为非登录 shell 里没有它）；
  5. rm 必须带 confirm=true —— 工具层强制，不让模型靠自觉。
"""
from __future__ import annotations

import os
import random
import re
import shlex
import shutil
import string
import subprocess
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
LOGIN_SH = SKILL_DIR / "scripts" / "login.sh"
SESSION_FILE = SKILL_DIR / ".session_id"
LOG_DIR = Path("/tmp")

_MAX_OUT = 6000
_DEFAULT_TIMEOUT = 180.0
_MAX_TIMEOUT = 1800.0

# 这几个子命令不在通用通道放行：登录必须走脚本，其余需用户明确指令
_REFUSED = {
    "login": "登录固定走 bdpan_login（内部调上游 scripts/login.sh），不裸调 bdpan login。",
    "logout": "注销会清掉授权，只在用户明确要求时执行；让用户自己跑 bdpan logout 或明确说“注销网盘登录”。",
    "update": "更新只在用户明确指令时做，用 bash scripts/update.sh，不走通用通道。",
    "install": "自安装/版本注册由 scripts/install.sh 负责，不在通用通道放行。",
}

_PROMPT = (
    "【技能 百度网盘 bdpan】网盘操作一律通过 bdpan 工具（远端路径都相对应用根目录 /apps/bdpan/，"
    "别写绝对路径）。开工先 bdpan_status 看登录态：未登录就把 bdpan_login() 返回的授权链接给用户，"
    "拿到 32 位授权码后调 bdpan_login(code=\"...\")。"
    "常用：ls <目录> --json / search <关键词> --json / upload <本地> <远端文件名> / download <远端> <本地> "
    "/ mkdir <目录> / share <路径> -d 7 / transfer <分享链接> [-p 提取码] / mv / cp / rename。"
    "公共参数 --agentname/--session-id/--session-input 由工具自动注入，别手写。"
    "铁律：rm 必须先列清单等用户确认再传 confirm=true；单文件上传远端路径不能以 / 结尾；"
    "ls/search 带 --json，并把返回的 return_markdown 原样输出给用户。"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bdpan",
            "description": (
                "执行百度网盘 bdpan 命令（ls/search/upload/download/mkdir/cp/mv/rename/share/transfer/whoami 等）。"
                "远端路径相对 /apps/bdpan/。公共参数自动注入；login/logout/update/install 不放行；rm 必须带 confirm=true。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "命令参数数组，不含 bdpan 本身。例：[\"ls\",\"/\",\"--json\"]、"
                            "[\"search\",\"体检报告\",\"--json\"]、[\"upload\",\"/tmp/a.pdf\",\"a.pdf\"]、"
                            "[\"share\",\"资料/x.pdf\",\"-d\",\"7\"]"
                        ),
                    },
                    "session_input": {
                        "type": "string",
                        "description": "本轮用户原始输入（逐字复制，用于服务质量追踪；不填则留空）",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "仅 rm 需要：用户已明确确认删除清单后传 true",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "超时秒数（默认 180，上限 1800）；大文件上传/下载建议配 background=true",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "大文件传输时传 true：后台跑并立刻返回 PID 与日志路径，随后用 tail 查进度",
                    },
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bdpan_status",
            "description": "检查百度网盘技能状态：bdpan 是否安装、版本、二进制路径、当前是否已登录。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bdpan_login",
            "description": (
                "百度网盘登录（OOB 授权）。不带 code：返回授权链接，让用户在浏览器打开；"
                "带 code：用浏览器显示的 32 位授权码完成登录。内部走上游 scripts/login.sh，不裸调 bdpan login。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "浏览器授权后显示的 32 位十六进制授权码；不传则只取授权链接",
                    }
                },
            },
        },
    },
]


# ---------- 基础设施 ----------

def _bdpan_bin() -> str | None:
    """定位 bdpan：官方安装器落在 ~/.local/bin，非登录 shell 的 PATH 里通常没有它。"""
    local = Path.home() / ".local" / "bin" / "bdpan"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("bdpan")


def _env() -> dict:
    env = dict(os.environ)
    bindir = str(Path.home() / ".local" / "bin")
    if bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _session_id() -> str:
    """{时间戳}-{6位随机}，同一技能进程/半天内复用（上游要求同会话内保持稳定）。"""
    try:
        if SESSION_FILE.exists() and time.time() - SESSION_FILE.stat().st_mtime < 12 * 3600:
            sid = SESSION_FILE.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"\d{10}-[A-Za-z0-9]{6}", sid):
                return sid
    except OSError:
        pass
    sid = f"{int(time.time())}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
    try:
        SESSION_FILE.write_text(sid, encoding="utf-8")
    except OSError:
        pass
    return sid


def _as_list(v) -> list[str]:
    """模型常把数组传成字符串（'a b' 或 JSON 串），都接住。"""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v).strip()
    if s.startswith("["):
        try:
            import json

            obj = json.loads(s)
            if isinstance(obj, list):
                return [str(x) for x in obj]
        except Exception:
            pass
    try:
        return shlex.split(s)
    except Exception:
        return s.split()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "是", "on")


def _common_args(session_input: str) -> list[str]:
    tail = ["--agentname", "dabai", "--session-id", _session_id()]
    if session_input:
        tail += ["--session-input", session_input]
    return tail


def _run(cmd: list[str], timeout: float, stdin_text: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(),
            cwd=str(SKILL_DIR),
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, f"（超时 {timeout:.0f}s 被中止）\n{partial[-1500:]}"
    except FileNotFoundError:
        return 127, f"找不到可执行文件：{cmd[0]}"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if err:
        out = (out + "\n[stderr]\n" + err).strip()
    return p.returncode, out


def _clip(text: str, limit: int = _MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出共 {len(text)} 字符，已截断）"


def _pretty(cmd: list[str]) -> str:
    return "bdpan " + " ".join(shlex.quote(a) for a in cmd)


# ---------- 工具实现 ----------

def do_bdpan(args: dict) -> str:
    binary = _bdpan_bin()
    if not binary:
        return ("bdpan 未安装。先让用户确认，再执行："
                "bash /home/wxf/dabai/skills/baidu-drive/scripts/install.sh --yes")

    cmd = _as_list(args.get("args"))
    if not cmd:
        return '缺少 args。例：{"args":["ls","/","--json"]}、{"args":["search","关键词","--json"]}'

    sub = next((t for t in cmd if not t.startswith("-")), "")
    if sub in _REFUSED:
        return f"拒绝执行 bdpan {sub}：{_REFUSED[sub]}"
    if sub == "rm" and not _truthy(args.get("confirm")):
        return ("rm 需要用户明确确认：先把待删对象（完整路径 + 文件/目录 + 数量）列给用户，"
                "说明不可逆、目录会连带内容，用户确认后再传 confirm=true 重调。")
    if sub == "rm" and "--force" not in cmd and "-f" not in cmd:
        cmd = cmd + ["--force"]  # 非交互环境没有 TTY，CLI 的交互确认会直接挂住

    full = [binary] + cmd + _common_args(str(args.get("session_input") or ""))
    timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT)
    timeout = max(10.0, min(timeout, _MAX_TIMEOUT))

    if _truthy(args.get("background")):
        log = LOG_DIR / f"bdpan-{sub or 'cmd'}-{int(time.time())}.log"
        try:
            with open(log, "wb") as fh:
                proc = subprocess.Popen(
                    full, stdout=fh, stderr=subprocess.STDOUT,
                    env=_env(), cwd=str(SKILL_DIR), start_new_session=True,
                )
        except OSError as e:
            return f"后台启动失败：{e}"
        return (f"已在后台启动（PID {proc.pid}）：{_pretty(cmd)}\n"
                f"日志：{log}\n查进度：kill -0 {proc.pid} 2>/dev/null && echo running || echo done; tail -5 {log}")

    rc, out = _run(full, timeout)
    body = out or "（无输出）"
    if rc != 0:
        body = f"（退出码 {rc}）\n{body}"
    return _clip(f"$ {_pretty(cmd)}\n{body}")


def do_status(args: dict) -> str:
    binary = _bdpan_bin()
    if not binary:
        return ("bdpan 未安装（不在 PATH，也没有 ~/.local/bin/bdpan）。"
                "安装：bash /home/wxf/dabai/skills/baidu-drive/scripts/install.sh --yes")
    _, ver = _run([binary, "version"], 60)
    rc, who = _run([binary, "whoami"], 60)
    logged_in = "已登录" in who and "未登录" not in who
    lines = [f"二进制：{binary}", ver.splitlines()[0] if ver else "版本未知"]
    lines.append(("登录态：" + ("已登录" if logged_in else "未登录")))
    if not logged_in:
        lines.append("→ 调 bdpan_login() 拿授权链接给用户。")
    return "\n".join(lines)


def do_login(args: dict) -> str:
    binary = _bdpan_bin()
    if not binary:
        return "bdpan 未安装，先跑 scripts/install.sh。"
    if not LOGIN_SH.exists():
        return f"缺少上游登录脚本：{LOGIN_SH}"

    code = str(args.get("code") or "").strip()
    if code:
        if not re.fullmatch(r"[a-fA-F0-9]{32}", code):
            return "授权码格式不对：应为 32 位十六进制字符，请让用户从浏览器授权结果页完整复制。"
        rc, out = _run(["bash", str(LOGIN_SH)], 180, stdin_text=f"y\n{code}\n")
        safe = out.replace(code, "[授权码已隐藏]")
        if rc == 0:
            _, who = _run([binary, "whoami"], 60)
            return _clip(f"登录成功。\n{who.strip()}")
        return _clip(f"登录失败（退出码 {rc}）：\n{safe}")

    rc, out = _run(["bash", str(LOGIN_SH)], 120, stdin_text="y\n")
    urls = re.findall(r"https?://[^\s\)\]]+", out)
    url = urls[0] if urls else ""
    if not url:
        return _clip(f"没拿到授权链接（退出码 {rc}）：\n{out}")
    return (f"[👉 点击此处打开授权页面]({url})\n\n"
            "链接 10 分钟内有效；授权后浏览器会显示 32 位授权码，把它发我，我调 bdpan_login(code=...) 完成登录。")


HANDLERS = {
    "bdpan": do_bdpan,
    "bdpan_status": do_status,
    "bdpan_login": do_login,
}

PROMPT = _PROMPT
