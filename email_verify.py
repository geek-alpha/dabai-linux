"""邮箱验证码：注册前的邮箱所有权证明。

为什么验证前置到「建号之前」：
先建号再发验证邮件的话，未验证的账号已经占了用户名、已经能登录 —— 验证就成了装饰。
这里只有验过码才拿得到票据，没票据 register() 不执行。

票据是自签 HMAC（邮箱|过期|sig），不落库：一次注册一个，用完即弃；服务重启不影响
已发出的票据（签名密钥在 data/auth_secret 里，不在进程内存里）。

没配 data/smtp.json 时整条链路关闭（smtp_ready() 为 False）：注册页只留 GitHub 一条路。
宁可少一条路，也不给一个「验证码永远收不到」的假按钮。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import smtplib
import ssl
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
SMTP_FILE = DATA_DIR / "smtp.json"
SECRET_FILE = DATA_DIR / "auth_secret"

CODE_TTL = 300          # 验证码有效期：5 分钟
TICKET_TTL = 900        # 票据有效期：15 分钟（覆盖「收到码 → 填完密码提交」这段）
CODE_TRIES = 5          # 一个码最多错 5 次，超了作废重发
COOLDOWN = 60           # 同邮箱重发冷却
MAIL_PER_EMAIL = 5      # 同邮箱每小时
MAIL_PER_IP = 10        # 同 IP 每小时
MAIL_GLOBAL = 30        # 全站每小时（防换 IP 分散）
WINDOW = 3600
# 分隔符用 | —— 它不在邮箱合法字符集里，所以 rsplit 不会被邮箱本身里的点或加号切错。
SEP = "|"

log = logging.getLogger("bp.email")

_LOCK = threading.Lock()
_CODES: dict[str, dict] = {}     # 小写邮箱 -> {salt, hash, exp, tries, sent}
_SENT: dict[str, list] = {}      # 限速桶 -> [时间戳]


class MailError(Exception):
    """带用户可读中文原因的失败。"""

    def __init__(self, msg: str, code: str = "mail_error"):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def _secret() -> bytes:
    """与 auth_core 共用同一份密钥：同机同盘，多读一次文件比跨模块传私有变量干净。"""
    try:
        raw = SECRET_FILE.read_bytes().strip()
    except OSError:
        raw = b""
    return raw or b"bp-email-secret"


def smtp_cfg() -> Optional[dict]:
    """发信配置。读不出来 / 缺字段一律按「没配」处理：半配的 SMTP 只会报错给用户看。"""
    try:
        d = json.loads(SMTP_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("data/smtp.json 解析失败，邮箱注册按未配置处理")
        return None
    if not isinstance(d, dict):
        return None
    host = str(d.get("host") or "").strip()
    user = str(d.get("user") or "").strip()
    pw = str(d.get("pass") or "")
    if not (host and user and pw):
        return None
    return {
        "host": host,
        "port": int(d.get("port") or 465),
        "user": user,
        "pass": pw,
        "from": str(d.get("from") or user).strip(),
        "name": str(d.get("name") or "白头凤科技").strip(),
        "ssl": bool(d.get("ssl", True)),
    }


def smtp_ready() -> bool:
    return smtp_cfg() is not None


def _cooldown_left(key: str) -> int:
    now = time.time()
    hist = [t for t in _SENT.get(key, []) if now - t < WINDOW]
    _SENT[key] = hist
    if not hist:
        return 0
    return max(0, int(COOLDOWN - (now - hist[-1])))


def _gate(email: str, ip: str) -> None:
    """发信闸门：冷却 → 每邮箱 → 每 IP → 全局。

    顺序上先查冷却（最贵的滥用形态是反复轰炸同一个邮箱），再查量级。
    """
    now = time.time()
    left = _cooldown_left("e:" + email)
    if left:
        raise MailError(f"刚发过一封，{left} 秒后再试", "cooldown")
    checks = (("e:" + email, MAIL_PER_EMAIL, "这个邮箱今天发得太多了，稍后再试"),
              ("i:" + (ip or "?"), MAIL_PER_IP, "这台设备请求太频繁，稍后再试"),
              ("*", MAIL_GLOBAL, "系统发信额度用完了，稍后再试"))
    for key, limit, msg in checks:
        hist = [t for t in _SENT.get(key, []) if now - t < WINDOW]
        _SENT[key] = hist
        if len(hist) >= limit:
            raise MailError(msg, "rate_limited")


def _record(email: str, ip: str) -> None:
    now = time.time()
    for key in ("e:" + email, "i:" + (ip or "?"), "*"):
        _SENT.setdefault(key, []).append(now)


def _send_mail(to: str, code: str) -> None:
    c = smtp_cfg()
    if not c:
        raise MailError("服务器还没配置发信邮箱", "unconfigured")
    body = (f"你的注册验证码是：{code}\n\n"
            f"{CODE_TTL // 60} 分钟内有效。密码设置完成后即可用邮箱登录。\n"
            f"不是本人操作请直接忽略这封邮件。\n\n"
            f"—— 白头凤科技 BATTLE PHOENIX")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"白头凤科技 · 注册验证码 {code}", "utf-8")
    msg["From"] = formataddr((str(Header(c["name"], "utf-8")), c["from"]))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(c["from"].partition("@")[2] or "battlephoenix.tech"))
    try:
        if c["ssl"]:
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=20,
                                  context=ssl.create_default_context()) as s:
                s.login(c["user"], c["pass"])
                s.sendmail(c["from"], [to], msg.as_string())
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                s.login(c["user"], c["pass"])
                s.sendmail(c["from"], [to], msg.as_string())
    except smtplib.SMTPAuthenticationError:
        # 授权码错/过期是最常见的一种，单独认出来 —— 否则用户只会看到「发信失败」，
        # 管理员要去翻日志才知道是密码问题。
        log.error("SMTP 认证失败：检查 data/smtp.json 的 pass 是不是邮箱授权码（不是登录密码）")
        raise MailError("发信账号配置不对，请联系管理员", "smtp_auth")
    except Exception as e:  # 网络/端口/TLS 都可能，统一给用户一句能懂的话
        log.error(f"发信失败：{type(e).__name__}: {e}")
        raise MailError("验证码没发出去，稍后再试或联系管理员", "smtp_failed")


def _rough_email(s: str) -> str:
    """粗筛：严格的格式校验在 auth_core._check_name，这里只挡明显不是邮箱的输入。

    两处都做完整校验会重复一份正则，改一处忘一处就是漏洞面；这里只要「有 @、
    没空白、长度像样」，够了 —— 反正真地址不合法的话 SMTP 也会拒。
    """
    e = str(s or "").strip().lower()
    if not e or "@" not in e or len(e) > 254 or any(ch.isspace() for ch in e):
        raise MailError("邮箱格式不对，示例：you@example.com", "bad_email")
    return e


def send_code(email: str, ip: str = "") -> dict:
    """生成并发送验证码。同步发信（RPi 上一次 1~3 秒，前端转圈可接受）。"""
    e = _rough_email(email)
    if not smtp_ready():
        raise MailError("服务器还没配置发信邮箱，请用 GitHub 登录", "unconfigured")
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _LOCK:
        _gate(e, ip)
        salt = secrets.token_hex(8)
        _CODES[e] = {
            "salt": salt,
            "hash": hashlib.sha256((salt + code).encode()).hexdigest(),
            "exp": time.time() + CODE_TTL,
            "tries": 0,
            "sent": time.time(),
        }
        _record(e, ip)
        # 顺手清掉过期项：这个字典的键是邮箱，不清理的话只有攻击者能把它撑大
        now = time.time()
        for k in [k for k, v in _CODES.items() if v["exp"] < now]:
            _CODES.pop(k, None)
    try:
        _send_mail(e, code)
    except MailError:
        with _LOCK:
            _CODES.pop(e, None)     # 没发出去就不该留下一个「有效」的码
        raise
    log.info(f"验证码已发往 {e}")
    return {"ok": True, "expires_in": CODE_TTL, "cooldown": COOLDOWN}


def _sign(email: str, exp: int) -> str:
    body = f"{email}{SEP}{exp}"
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]


def check_code(email: str, code: str) -> str:
    """校验验证码，通过则返回一次性票据。失败抛 MailError。"""
    e = _rough_email(email)
    c = str(code or "").strip()
    with _LOCK:
        rec = _CODES.get(e)
        if not rec:
            raise MailError("先点「获取验证码」，或验证码已过期", "no_code")
        if rec["exp"] < time.time():
            _CODES.pop(e, None)
            raise MailError("验证码过期了，重新获取一个", "code_expired")
        if rec["tries"] >= CODE_TRIES:
            _CODES.pop(e, None)
            raise MailError("错误次数太多，重新获取一个", "too_many_tries")
        rec["tries"] += 1
        calc = hashlib.sha256((rec["salt"] + c).encode()).hexdigest()
        if not hmac.compare_digest(calc, rec["hash"]):
            raise MailError("验证码不对", "bad_code")
        _CODES.pop(e, None)         # 一次一码，用完即弃
    return make_ticket(e)


def make_ticket(email: str, ttl: int = TICKET_TTL) -> str:
    e = _rough_email(email)
    exp = int(time.time()) + int(ttl)
    return f"{e}{SEP}{exp}{SEP}{_sign(e, exp)}"


def verify_ticket(email: str, ticket: str) -> bool:
    """票据有效且属于这个邮箱。任何解析异常都当无效处理（不抛：调用方在注册主路径上）。"""
    try:
        e = _rough_email(email)
        parts = str(ticket or "").rsplit(SEP, 2)
        if len(parts) != 3:
            return False
        t_mail, exp_s, sig = parts
        if t_mail != e:
            return False
        if int(exp_s) < time.time():
            return False
        return hmac.compare_digest(sig, _sign(e, int(exp_s)))
    except Exception:
        return False
