"""多用户账号体系：注册 / 登录 / 会话 token / 失败限速。

为什么是这套：
- 纯标准库（hashlib/hmac/secrets/json），零新依赖——树莓派 1GB 内存，装一个包都是成本
- 密码 PBKDF2-HMAC-SHA256 12 万次迭代：Pi 3 上约 0.6~1.2s，登录可接受、爆破不可接受
- 会话 token 自签无状态（uid.ver.exp.sig）：服务重启不掉线，app 端才能长期免登录；
  口令版本号 ver 是这套方案唯一的吊销手段 —— 改密码时把它 +1，旧设备当场失效
- 用户表 tmp+rename 原子写盘：树莓派断电频繁，写一半的 json 会让所有人登不进来

token 形态：`u_ab12cd34.<口令版本>.<过期unix秒>.<hmac16>`
密钥落盘 data/auth_secret（0600）；删掉它 = 全体会话失效（用户表不受影响）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Optional

import email_verify

DATA_DIR = Path(__file__).resolve().parent / "data"
USERS_FILE = DATA_DIR / "users.json"
SECRET_FILE = DATA_DIR / "auth_secret"

PBKDF2_ROUNDS = 120_000
TOKEN_TTL = 180 * 24 * 3600  # app 要长期免登录，半年
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{2,24}$")  # \w 含下划线与字母数字
# 邮箱也当名字用：_name_key 那套「NFKC + 大小写折叠 + 忽略空白」的判重口径
# 正好就是邮箱需要的语义，所以不另建邮箱索引表 —— 一张表一套查找路径。
# 本地部分用经典的「点分段」写法：a..b@x.com 和 .a@x.com 这类畸形地址过不去。
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
_EMAIL_MAX = 254        # RFC 5321 总长上限
_EMAIL_LOCAL_MAX = 64   # 本地部分上限：它才是撞库爆破面，不靠总长间接卡
_MIN_PWD = 6

# 角色：admin 完整权限；user 只能在 data/sandboxes/<uid>/ 里读写和跑命令（见 sandbox.py）
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)
# 零宽空格/双向控制符：肉眼不可见，留着就能注册出「看起来同名」的账号来冒充
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")

# 登录失败限速（进程内存即可：单机单进程，重启清空无害）
_FAILS: dict[str, list] = {}
_FAIL_LIMIT = 5
_FAIL_WINDOW = 300
_LOCK_SECONDS = 60

# 注册闸门（进程内存即可，同上）。公网自助注册是唯一「不登录也能让 Pi 干活」的入口：
# 一次成功注册 = 一次 12 万轮 PBKDF2（Pi 3 上约 1s CPU）+ 一次 users.json 落盘。
# 不设闸门，一个循环脚本就能把 SD 卡写满、把 CPU 占死。
_REG_HIST: dict[str, list] = {}
_REG_PER_IP = 3      # 每 IP 每小时
_REG_GLOBAL = 10     # 全局每小时（防换 IP 分散）
_REG_WINDOW = 3600
MAX_USERS = 20
REG_OPEN_FILE = DATA_DIR / "registration.json"  # {"open": false} 关闭自助注册


class AuthError(Exception):
    """带用户可读中文原因的鉴权失败。"""

    def __init__(self, msg: str, code: str = "auth_error"):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _load_secret() -> bytes:
    if SECRET_FILE.exists():
        raw = SECRET_FILE.read_bytes().strip()
        if raw:
            return raw
    key = secrets.token_bytes(32)
    _atomic_write(SECRET_FILE, key.hex())
    return key.hex().encode()


_SECRET = _load_secret()


def _load_users() -> dict:
    if not USERS_FILE.exists():
        return {"users": {}}
    try:
        d = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}}
    if not isinstance(d, dict) or not isinstance(d.get("users"), dict):
        return {"users": {}}
    return d


def _save_users(d: dict) -> None:
    _atomic_write(USERS_FILE, json.dumps(d, ensure_ascii=False, indent=2), mode=0o600)


def _hash_pwd(pwd: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return dk.hex()


def _norm_name(name: str) -> str:
    """规范名：去首尾空白 + 去不可见字符，内部连续空白压成一个空格。"""
    n = _INVISIBLE_RE.sub("", str(name or ""))
    return " ".join(n.split()).strip()


def _name_key(name: str) -> str:
    """判重比较键：NFKC 归一化 + 大小写折叠 + 忽略空白。

    NFKC 把全角/半角与兼容字符折叠成同一形态（Ａ→A），所以 Alice / alice /
    Ａｌｉｃｅ / 王 幸凤 与 王幸凤 在判重上都是同一个名字——
    否则用全角字母或中间塞个空格就能注册出冒充账号。
    """
    n = unicodedata.normalize("NFKC", _norm_name(name))
    return "".join(n.split()).casefold()


def _check_name(name: str) -> str:
    n = _norm_name(name)
    if "@" in n:
        # fullmatch 而不是 match：match 只认前缀，`a@x.com.` 这种尾部垃圾会蒙混过关。
        if (len(n) > _EMAIL_MAX or len(n.partition("@")[0]) > _EMAIL_LOCAL_MAX
                or not _EMAIL_RE.fullmatch(n)):
            raise AuthError("邮箱格式不对，示例：you@example.com", "bad_email")
        # 统一存小写：Gmail/QQ 这类主流服务都不区分大小写，存原文会让
        # 用户列表里出现 Alice@x.com 与 alice@x.com 两个「看起来不同」的账号。
        return n.lower()
    if not _NAME_RE.match(n):
        raise AuthError("用户名 2~24 位，可用中文/字母/数字/下划线/短横线", "bad_name")
    return n


def _check_pwd(pwd: str) -> str:
    if not isinstance(pwd, str) or len(pwd) < _MIN_PWD:
        raise AuthError(f"密码至少 {_MIN_PWD} 位", "bad_pwd")
    if len(pwd) > 128:
        raise AuthError("密码过长", "bad_pwd")
    return pwd


def _find_by_name(users: dict, name: str) -> Optional[dict]:
    key = _name_key(name)
    for u in users.values():
        if _name_key(u.get("name", "")) == key:
            return u
    return None


def public_user(u: dict) -> dict:
    """对外暴露的用户视图（绝不含密码字段）。"""
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "role": u.get("role") or ROLE_USER,
        "created": u.get("created"),
        "last_login": u.get("last_login"),
        "device": u.get("device", ""),
        "gh": bool(u.get("github_id")),
        "email": u.get("email", ""),
        "email_verified": bool(u.get("email_verified")),
    }


def registration_open() -> bool:
    """自助注册是否开放。默认开放；data/registration.json 写 {"open": false} 关闭。

    配置读坏时按「开放」处理：闸门还有限速兜底，而误判成关闭会让新设备彻底注册不进来。
    """
    try:
        cfg = json.loads(REG_OPEN_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except Exception:
        return True
    return bool(cfg.get("open", True)) if isinstance(cfg, dict) else True


def user_count() -> int:
    return len(_load_users()["users"])


def _reg_gate(ip: str) -> None:
    """自助注册闸门：开关 → 每 IP 限速 → 全局限速。

    闸门排在名字/密码校验之前：否则攻击者用无效名字无限探用户名存在性，
    永远不消耗额度（探测走的正是 name_taken 这条分支）。
    """
    if not registration_open():
        raise AuthError("注册已关闭，请联系管理员", "registration_closed")
    now = time.time()
    for key, limit in ((ip, _REG_PER_IP), ("*", _REG_GLOBAL)):
        hist = [t for t in _REG_HIST.get(key, []) if now - t < _REG_WINDOW]
        _REG_HIST[key] = hist
        if len(hist) >= limit:
            raise AuthError("注册太频繁，请稍后再试", "rate_limited")


def _reg_record(ip: str) -> None:
    """只记成功的那次：贵的只有成功路径（PBKDF2 + 写盘）。"""
    _REG_HIST.setdefault(ip, []).append(time.time())
    _REG_HIST.setdefault("*", []).append(time.time())


def register(name: str, pwd: str, device: str = "", role: str = "", ip: str = "",
             github_id: int = 0, github_login: str = "", email_ticket: str = "") -> dict:
    """注册账号。role 留空 = 普通用户；用户表为空时第一个账号自动成为管理员。

    ip 非空 = 来自 HTTP 自助注册：走闸门（开关 + 限速），且只认两条身份来源 ——
    验过码的邮箱，或 GitHub。用户名自助注册已关闭（见下面的闸门）。
    进程内调用（管理员建号、自检）不传 ip，既不受限速也不受身份来源约束。

    email_ticket 由 email_verify.check_code() 在校验验证码后签发，是「这个邮箱
    确实是他的」的唯一凭据。
    """
    d = _load_users()
    users = d["users"]
    if len(users) >= MAX_USERS:
        raise AuthError(f"用户数已达上限（{MAX_USERS}），请联系管理员", "too_many_users")
    if ip:
        _reg_gate(ip)
    n = _check_name(name)
    # 自助注册只剩两条路：邮箱（验过码）或 GitHub。没有验证手段的账号等于一个
    # 匿名可弃的身份 —— 出事时连人都找不到，也永远做不了「邮箱找回密码」。
    email_ok = False
    if ip and not github_id:
        if "@" not in n:
            raise AuthError("请用邮箱注册（收验证码），或直接用 GitHub 登录", "email_required")
        email_ok = email_verify.verify_ticket(n, email_ticket)
        if not email_ok:
            raise AuthError("邮箱还没验证，请先点「获取验证码」", "email_unverified")
    _check_pwd(pwd)
    if _find_by_name(users, n):
        raise AuthError("这个用户名已被占用", "name_taken")
    if role not in ROLES:
        role = ROLE_ADMIN if not users else ROLE_USER
    uid = "u_" + secrets.token_hex(4)
    salt = secrets.token_hex(16)
    now = int(time.time())
    users[uid] = {
        "id": uid,
        "name": n,
        "role": role,
        "salt": salt,
        "pwd": _hash_pwd(pwd, salt),
        "created": now,
        "last_login": now,
        "device": (device or "")[:64],
    }
    if "@" in n:
        # 自助注册走到这里必然验过码（上面的闸门拦着），所以 email_ok 为真；
        # 只有管理员手工建的邮箱账号才是未验证。这个标志决定以后能不能走
        # 「邮箱找回密码」—— 未验证的邮箱 + 找回 = 谁输入你的邮箱谁就能改你密码。
        users[uid]["email"] = n
        users[uid]["email_verified"] = email_ok
    if github_id:
        users[uid]["github_id"] = int(github_id)
        users[uid]["github_login"] = str(github_login or "")[:40]
    _save_users(d)
    if ip:
        _reg_record(ip)
    return public_user(users[uid])


def _rate_gate(key: str) -> None:
    now = time.time()
    hist = [t for t in _FAILS.get(key, []) if now - t < _FAIL_WINDOW]
    _FAILS[key] = hist
    if len(hist) >= _FAIL_LIMIT and now - hist[-1] < _LOCK_SECONDS:
        wait = int(_LOCK_SECONDS - (now - hist[-1])) + 1
        raise AuthError(f"密码错误次数过多，请 {wait} 秒后再试", "rate_limited")


def _rate_fail(key: str) -> None:
    _FAILS.setdefault(key, []).append(time.time())


def login(name: str, pwd: str, device: str = "") -> dict:
    n = _norm_name(name)
    key = n.lower()
    _rate_gate(key)
    d = _load_users()
    u = _find_by_name(d["users"], n)
    ok = False
    if u:
        calc = _hash_pwd(pwd if isinstance(pwd, str) else "", u.get("salt", ""))
        ok = hmac.compare_digest(calc, u.get("pwd", ""))
    if not ok:
        _rate_fail(key)
        raise AuthError("用户名或密码不对", "bad_credentials")
    _FAILS.pop(key, None)
    u["last_login"] = int(time.time())
    if device:
        u["device"] = device[:64]
    _save_users(d)
    return public_user(u)


def get_user(uid: str) -> Optional[dict]:
    u = _load_users()["users"].get(uid)
    return public_user(u) if u else None


def list_users() -> list:
    return [public_user(u) for u in _load_users()["users"].values()]


def get_role(uid: str) -> str:
    """用户角色；用户不存在返回空串（调用方据此判定「不是本系统用户」）。"""
    u = _load_users()["users"].get(str(uid or ""))
    if not u:
        return ""
    return str(u.get("role") or ROLE_USER)


def is_admin(uid: str) -> bool:
    return get_role(uid) == ROLE_ADMIN


def find_by_name(name: str) -> Optional[dict]:
    """按名字查用户（与判重同一口径）。"""
    u = _find_by_name(_load_users()["users"], name)
    return public_user(u) if u else None


def find_by_github(gid: int) -> Optional[dict]:
    """按 GitHub 数字 id 找账号。id 改不了，用户名能改 —— 认身份只认 id。"""
    try:
        gid = int(gid or 0)
    except (TypeError, ValueError):
        return None
    if gid <= 0:
        return None
    for u in _load_users()["users"].values():
        try:
            if int(u.get("github_id") or 0) == gid:
                return public_user(u)
        except (TypeError, ValueError):
            continue
    return None


def claim_github(gid: int, login: str) -> dict:
    """GitHub 首次登录：认领同名账号，没有同名才新建普通用户。

    认领而非新建是刻意的 —— 管理员号先于 GitHub 存在，再建一个的话，
    本人 GitHub 登录进来会变成没有权限的陌生人。
    口令给 24 字节随机串：这号没有本地口令，只能从 GitHub 进。
    """
    try:
        gid = int(gid or 0)
    except (TypeError, ValueError):
        gid = 0
    if gid <= 0:
        raise AuthError("GitHub 身份无效", "bad_github")
    u = find_by_github(gid)
    if u:
        return u
    d = _load_users()
    same = _find_by_name(d["users"], login)
    if same:
        same["github_id"] = gid
        same["github_login"] = str(login or "")[:40]
        _save_users(d)
        return public_user(same)
    return register(login, secrets.token_urlsafe(24), github_id=gid, github_login=login)


def set_role(uid: str, role: str) -> dict:
    """改角色（管理员授予/回收）。返回更新后的公开视图。"""
    if role not in ROLES:
        raise AuthError(f"未知角色：{role}", "bad_role")
    d = _load_users()
    u = d["users"].get(str(uid or ""))
    if not u:
        raise AuthError("用户不存在", "no_user")
    u["role"] = role
    _save_users(d)
    return public_user(u)


def reset_password(uid: str, pwd: str, email_ticket: str = "") -> dict:
    """用邮箱验证码重置密码。

    票据是唯一门槛，所以只认「邮箱已验证」的账号：未验证的邮箱谁都能填，
    放行就等于把账号送给知道你邮箱的人（email_verified 就是为这条存在的）。
    """
    d = _load_users()
    u = d["users"].get(str(uid or ""))
    if not u:
        raise AuthError("用户不存在", "no_user")
    email = str(u.get("email") or "")
    if not (email and u.get("email_verified")):
        raise AuthError("这个账号没有验证过的邮箱，没法自助找回密码", "email_unverified")
    if not email_verify.verify_ticket(email, email_ticket):
        raise AuthError("邮箱还没验证，请先点「获取验证码」", "email_unverified")
    _check_pwd(pwd)
    salt = secrets.token_hex(16)
    u["salt"] = salt
    u["pwd"] = _hash_pwd(pwd, salt)
    # 改密码就得让旧会话作废：邮箱验码只证明「填表的人能收信」，
    # 不代表没别人拿着旧 cookie 待在这账号里（共用电脑、被偷的手机）。
    u["pwd_ver"] = _token_ver(u) + 1
    u["pwd_changed"] = int(time.time())
    _save_users(d)
    # 忘了密码的人多半已经试到被锁，改完还锁着就是「重置了还是进不去」
    _FAILS.pop(_name_key(u.get("name", "")), None)
    return public_user(u)


def delete_user(uid: str, pwd: str = "") -> dict:
    """注销账号：从用户表移除，返回被删的公开视图。

    有本地口令的账号必须再输一次口令 —— cookie 可能是别人机器上没退出的会话。
    最后一个管理员不许注销：删掉就再没人能进管理端，只能手改 json 救回来。
    """
    d = _load_users()
    users = d["users"]
    u = users.get(str(uid or ""))
    if not u:
        raise AuthError("用户不存在", "no_user")
    key = _name_key(u.get("name", ""))
    if u.get("pwd") and u.get("salt"):
        _rate_gate(key)
        calc = _hash_pwd(pwd if isinstance(pwd, str) else "", u["salt"])
        if not hmac.compare_digest(calc, u.get("pwd", "")):
            _rate_fail(key)
            raise AuthError("密码不对", "bad_credentials")
        _FAILS.pop(key, None)
    if str(u.get("role") or ROLE_USER) == ROLE_ADMIN:
        rest = [x for k, x in users.items()
                if k != u["id"] and str(x.get("role") or "") == ROLE_ADMIN]
        if not rest:
            raise AuthError("这是最后一个管理员账号，不能注销", "last_admin")
    del users[u["id"]]
    _save_users(d)
    _FAILS.pop(key, None)
    return public_user(u)



def _token_ver(u: dict) -> int:
    """账号当前的口令版本号；老账号没有这个字段，按 0 算。"""
    try:
        return int(u.get("pwd_ver") or 0)
    except (TypeError, ValueError):
        return 0


def make_token(uid: str, ttl: int = TOKEN_TTL) -> str:
    """签发会话 token：`uid.<口令版本>.<过期秒>.<hmac>`。

    版本号取签发现刻账号的值。代价是签发时读一次用户表 —— 登录路径本来就读过，
    而 verify_token 每个请求都要查用户，那一次读是省不掉的。
    """
    u = _load_users()["users"].get(str(uid or "")) or {}
    exp = int(time.time()) + int(ttl)
    body = f"{uid}.{_token_ver(u)}.{exp}"
    sig = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """有效则返回 uid，否则 None（不抛异常：中间件每个请求都要调）。

    两种形态都认：4 段带口令版本号（当前），3 段是加版本号之前签发的 —— 那种按版本 0
    处理，所以「改过密码的老账号」手里的旧 token 会立刻失效，而没改过密码的人不掉线。
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) == 3:  # 老形态：签名覆盖的只有 uid.exp，版本按 0 算
        uid, ver_s, exp_s = parts[0], "0", parts[1]
        body = f"{uid}.{exp_s}"
    elif len(parts) == 4:
        uid, ver_s, exp_s = parts[0], parts[1], parts[2]
        body = f"{uid}.{ver_s}.{exp_s}"
    else:
        return None
    expect = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(parts[-1], expect):
        return None
    try:
        if int(exp_s) < time.time():
            return None
        ver = int(ver_s)
    except ValueError:
        return None
    u = _load_users()["users"].get(uid)
    if not u or _token_ver(u) != ver:
        return None
    return uid
