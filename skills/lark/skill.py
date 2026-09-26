# -*- coding: utf-8 -*-
"""飞书技能实现 —— 官方 lark-cli 的 dabai 适配层。

上游：https://github.com/larksuite/cli （larksuite 官方维护，MIT）
上游形态：Go 写的 CLI，二进制里内嵌 28 个 Agent Skill（SKILL.md + references/），
由 Agent 自己拼 shell 命令。这里收成 4 个工具：

  lark         透传命令（读走这里；高风险写必须用户确认后 confirm=true）
  lark_status  版本 + doctor + auth status
  lark_login   两步式授权：mode=app 建/绑应用；mode=auth 拿 URL，finish 收尾
  lark_skill   读内嵌技能文档（skills list / skills read）

上游契约在本层的落地（依据 AGENTS.md 与 lark-shared/SKILL.md）：
  1. 成功判定用 ok == true，不是 code == 0（成功信封没有顶层 code 字段）；
  2. high-risk-write 命令需要 --yes：本层剥掉模型自己加的 --yes，必须用户确认后 confirm=true；
  3. 授权 URL 原样转发，不改 query、不加标点；
  4. 不打印密钥/凭据；config init 走 --new 由浏览器完成，本层不碰 app_secret。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
DEVICE_FILE = SKILL_DIR / ".device_code"
INIT_LOG = SKILL_DIR / ".config-init.log"
LOG_DIR = Path("/tmp")

_MAX_OUT = 6000
_DEFAULT_TIMEOUT = 180.0
_MAX_TIMEOUT = 1800.0

# 这些子命令必须走专用工具，不走通用通道（键是命令前 1~2 个非 flag token）
_REFUSED = {
    ("auth", "login"): "授权登录走 lark_login（两步式：先拿链接、授权后 finish），不阻塞对话。",
    ("auth", "logout"): "注销会清掉凭据。只在用户明确要求时执行——让用户自己跑 lark-cli auth logout。",
    ("config", "init"): '初始化/新建应用走 lark_login(mode="app")。',
    ("config", "bind"): ("绑定已有应用凭据要先定身份策略（--identity bot-only / user），"
                         "让用户确认后自己跑 lark-cli config bind。"),
    ("update",): "更新 CLI 只在用户明确指令时做，不走通用通道。",
}

_PROMPT = (
    "【技能 飞书 lark-cli】飞书操作一律走 lark 工具（larksuite 官方 CLI，覆盖 im/calendar/docs/sheets/base/task/mail/drive 等域）。"
    "动手前先 lark_status 看配置与登录态：未配置用 lark_login(mode=\"app\")，未登录用 lark_login(mode=\"auth\")，"
    "把返回的授权链接原样给用户，用户说完成后调 lark_login(mode=\"auth\", finish=true)。"
    "命令用法别猜：lark_skill(action=\"list\") 看 28 个域技能，lark_skill(action=\"read\", name=\"lark-im\") 读该域全文，"
    "或 lark(args=[\"im\",\"--help\"]) / lark(args=[\"schema\",\"im.message.create\"])。"
    "优先用 +shortcut（如 [\"calendar\",\"+agenda\"]）而不是裸 API 资源。"
    "铁律：成功判定看 ok==true；高风险写命令（--help 标 high-risk-write）要 --yes，先向用户确认再传 confirm=true；"
    "删除/发送/覆盖类操作先列清单等确认。"
)


# ---------- 基础设施 ----------

def _bin() -> str | None:
    """定位 lark-cli：npm 用户级安装落在 ~/.local/bin，非登录 shell 的 PATH 里通常没有。"""
    local = Path.home() / ".local" / "bin" / "lark-cli"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("lark-cli")


def _env() -> dict:
    env = dict(os.environ)
    bindir = str(Path.home() / ".local" / "bin")
    if bindir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "是", "on")


def _as_list(v) -> list[str]:
    """模型常把数组传成字符串（'a b' 或 JSON 串），都接住。"""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v).strip()
    if s.startswith("["):
        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                return [str(x) for x in obj]
        except Exception:
            pass
    try:
        return shlex.split(s)
    except Exception:
        return s.split()


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=_env(), cwd=str(SKILL_DIR))
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
    return "lark-cli " + " ".join(shlex.quote(a) for a in cmd)


def _ok(out: str) -> bool:
    """上游契约：成功判定看 ok == true，不是 code == 0。"""
    obj = _json_of(out)
    return isinstance(obj, dict) and obj.get("ok") is True


def _json_of(out: str):
    head = out.split("\n[stderr]")[0].strip()
    try:
        return json.loads(head)
    except Exception:
        return None


def _dig(obj, keywords: tuple[str, ...]) -> str:
    """在 JSON 信封里递归找第一个键名含关键字的非空字符串值。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v and any(w in k.lower() for w in keywords):
                return v
        for v in obj.values():
            r = _dig(v, keywords)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig(v, keywords)
            if r:
                return r
    return ""


_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")


def _find_url(text: str) -> str:
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)  # 去掉 ANSI 转义
    urls = _URL_RE.findall(clean)
    if not urls:
        return ""
    for u in urls:
        if any(k in u for k in ("verification", "device", "accounts", "open.feishu", "open.larksuite")):
            return u
    return urls[-1]


# ---------- 工具实现 ----------

def do_lark(args: dict) -> str:
    binary = _bin()
    if not binary:
        return ("lark-cli 未安装。装法：npm install -g --prefix ~/.local @larksuite/cli"
                "（装到 /usr/local 会 EACCES）。")

    cmd = _as_list(args.get("args"))
    if not cmd:
        return ('缺少 args。例：{"args":["calendar","+agenda"]}、'
                '{"args":["im","+send","--receive-id","ou_xxx","--text","hi"]}、'
                '{"args":["schema","im.message.create"]}')

    plain = [t for t in cmd if not t.startswith("-")]
    key1 = tuple(plain[:1])
    key2 = tuple(plain[:2])
    reason = _REFUSED.get(key2) or _REFUSED.get(key1)
    if reason:
        return f"拒绝执行 lark-cli {' '.join(plain[:2])}：{reason}"

    stripped_yes = False
    if "--yes" in cmd and not _truthy(args.get("confirm")):
        cmd = [t for t in cmd if t != "--yes"]
        stripped_yes = True

    timeout = max(10.0, min(float(args.get("timeout") or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
    full = [binary] + cmd

    if _truthy(args.get("background")):
        log = LOG_DIR / f"lark-{(plain[0] if plain else 'cmd')}-{int(time.time())}.log"
        try:
            with open(log, "wb") as fh:
                proc = subprocess.Popen(full, stdout=fh, stderr=subprocess.STDOUT,
                                        env=_env(), cwd=str(SKILL_DIR), start_new_session=True)
        except OSError as e:
            return f"后台启动失败：{e}"
        return (f"已在后台启动（PID {proc.pid}）：{_pretty(cmd)}\n日志：{log}\n"
                f"查进度：kill -0 {proc.pid} 2>/dev/null && echo running || echo done; tail -5 {log}")

    rc, out = _run(full, timeout)
    body = out or "（无输出）"
    if rc != 0:
        body = f"（退出码 {rc}）\n{body}"
    if stripped_yes:
        body = ("（已剥掉 --yes：高风险写操作需要用户确认。把将要执行的命令与影响讲给用户，"
                "用户同意后再传 confirm=true。）\n" + body)
    return _clip(f"$ {_pretty(cmd)}\n{body}")


def do_status(args: dict) -> str:
    binary = _bin()
    if not binary:
        return "lark-cli 未安装。装法：npm install -g --prefix ~/.local @larksuite/cli"
    _, ver = _run([binary, "--version"], 30)
    _, doc = _run([binary, "doctor"], 90)
    _, auth = _run([binary, "auth", "status", "--json"], 60)

    vline = (ver.strip().splitlines() or ["版本未知"])[0] if ver.strip() else "版本未知"
    lines = [f"二进制：{binary}", vline]

    dobj = _json_of(doc)
    if isinstance(dobj, dict) and dobj.get("checks"):
        for c in dobj["checks"]:
            mark = "✓" if c.get("status") == "pass" else "✗"
            line = f"{mark} {c.get('name')}: {c.get('message')}"
            if c.get("hint"):
                line += f"\n   提示：{c['hint']}"
            lines.append(line)
    else:
        lines.append(_clip(doc, 1200))

    aobj = _json_of(auth)
    if isinstance(aobj, dict) and aobj.get("ok") is True:
        ident = _dig(aobj, ("identity", "identity_type"))
        who = _dig(aobj, ("user_name", "name", "email", "user_id", "open_id"))
        lines.append(f"登录态：已登录{'（身份 ' + ident + '）' if ident else ''}"
                     f"{'（' + who + '）' if who else ''}")
    else:
        msg = ""
        if isinstance(aobj, dict) and isinstance(aobj.get("error"), dict):
            err = aobj["error"]
            msg = str(err.get("message") or err.get("hint") or "")
        lines.append(f"登录态：未登录/未配置{'（' + msg + '）' if msg else ''}")
        lines.append('→ 配置应用：lark_login(mode="app")；用户授权：lark_login(mode="auth")。')
    return "\n".join(lines)


def _login_app(binary: str) -> str:
    """建/绑应用：config init --new 会阻塞等浏览器，所以后台跑 + 轮询日志抓 URL。"""
    try:
        INIT_LOG.unlink()
    except OSError:
        pass
    try:
        fh = open(INIT_LOG, "wb")
    except OSError as e:
        return f"无法写日志文件 {INIT_LOG}：{e}"
    try:
        proc = subprocess.Popen([binary, "config", "init", "--new"], stdout=fh,
                                stderr=subprocess.STDOUT, env=_env(), cwd=str(SKILL_DIR),
                                start_new_session=True)
    except OSError as e:
        fh.close()
        return f"启动 config init 失败：{e}"

    url, text = "", ""
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            text = INIT_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        url = _find_url(text)
        if url or proc.poll() is not None:
            break
    fh.close()

    if url:
        return (f"[👉 点击此处打开授权页面]({url})\n\n"
                "在浏览器里用你的飞书账号完成应用创建/授权，配置会自动写入 lark-cli。\n"
                f"命令还在后台等你完成（PID {proc.pid}，日志 {INIT_LOG}）；完成后告诉我，我跑 lark_status 确认。")
    tail = _clip(text[-1500:]) if text else "（日志为空）"
    return (f"没抓到授权链接（PID {proc.pid}，退出码 {proc.poll()}）。日志尾部：\n{tail}\n"
            f"可手动查看：tail -20 {INIT_LOG}")


def _login_auth(binary: str, args: dict) -> str:
    """用户授权：--no-wait 拿 verification URL + device code，用户完成后再 finish 收尾。"""
    code = str(args.get("device_code") or "").strip()
    if not code:
        try:
            code = DEVICE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            code = ""

    if _truthy(args.get("finish")):
        if not code:
            return ('没有待完成的 device code。先调 lark_login(mode="auth") 发起授权，'
                    "用户授权后再 finish=true。")
        rc, out = _run([binary, "auth", "login", "--device-code", code, "--json"], 300)
        if _ok(out):
            _, auth = _run([binary, "auth", "status", "--json"], 60)
            return _clip("授权完成。\n" + (auth or out))
        return _clip(f"授权收尾未成功（退出码 {rc}）：\n{out}")

    domains = _as_list(args.get("domains"))
    cmd = [binary, "auth", "login"]
    cmd += ["--domain", ",".join(domains)] if domains else ["--recommend"]
    cmd += ["--no-wait", "--json"]
    rc, out = _run(cmd, 120)
    obj = _json_of(out)
    url = _dig(obj, ("verification_url", "verification_uri", "url")) if obj else _find_url(out)
    device = _dig(obj, ("device_code",)) if obj else ""
    if device:
        try:
            DEVICE_FILE.write_text(device, encoding="utf-8")
        except OSError:
            pass
    if not url:
        return _clip(f"没拿到授权链接（退出码 {rc}）：\n{out}\n"
                     '（若提示未配置，先 lark_login(mode="app") 建应用。）')
    scope_note = f"申请范围：{' '.join(domains)}" if domains else "申请范围：全部业务域（--recommend）"
    return (f"[👉 点击此处打开授权页面]({url})\n\n"
            f"{scope_note}。链接原样转发给用户，别改参数。\n"
            '用户在浏览器完成授权后告诉我一声，我调 lark_login(mode="auth", finish=true) 收尾。')


def do_login(args: dict) -> str:
    binary = _bin()
    if not binary:
        return "lark-cli 未安装。装法：npm install -g --prefix ~/.local @larksuite/cli"
    mode = str(args.get("mode") or "app").strip().lower()
    if mode in ("app", "config", "init", "app-new"):
        return _login_app(binary)
    if mode in ("auth", "user", "login"):
        return _login_auth(binary, args)
    return f'mode 只支持 "app"（建/绑应用）或 "auth"（用户授权），收到的是 {mode!r}'


def do_skill(args: dict) -> str:
    binary = _bin()
    if not binary:
        return "lark-cli 未安装。装法：npm install -g --prefix ~/.local @larksuite/cli"
    action = str(args.get("action") or "list").strip().lower()
    if action in ("list", "ls"):
        rc, out = _run([binary, "skills", "list"], 90)
        obj = _json_of(out)
        if isinstance(obj, dict) and obj.get("skills"):
            lines = []
            for s in obj["skills"]:
                d = " ".join(str(s.get("description") or "").split())
                if len(d) > 70:
                    d = d[:70] + "…"
                lines.append(f"{s.get('name')} — {d}")
            lines.append('')
            lines.append('读某个域全文：lark_skill(action="read", name="lark-im")')
            return "\n".join(lines)
        if rc != 0 and not out:
            return f"skills list 失败（退出码 {rc}）"
        return _clip(out, 3000)
    if action in ("read", "cat", "show"):
        name = str(args.get("name") or "").strip()
        if not name:
            return 'read 需要 name，如 name="lark-im"。先 action="list" 看有哪些。'
        rc, out = _run([binary, "skills", "read", name], 90)
        if rc != 0 and not out:
            return f"skills read {name} 失败（退出码 {rc}）"
        return _clip(out, 12000)
    return f'action 只支持 "list" 或 "read"，收到的是 {action!r}'


HANDLERS = {
    "lark": do_lark,
    "lark_status": do_status,
    "lark_login": do_login,
    "lark_skill": do_skill,
}

PROMPT = _PROMPT
