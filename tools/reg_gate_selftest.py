"""注册闸门自检：用户名自助注册必须死，邮箱验码 / GitHub 必须活。

这一层测的是 auth_core 的核心闸门（不经 HTTP）；HTTP 层的闸门与限速由
tools/auth_http_selftest.py 覆盖。两者互补，都要跑。

用户库重定向到临时目录 —— 绝不碰真实 data/users.json。

用法：venv/bin/python tools/reg_gate_selftest.py
"""
import hashlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import auth_core          # noqa: E402
import email_verify       # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="regtest-"))
auth_core.USERS_FILE = TMP / "users.json"

OK = []
BAD = []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))


def expect_err(name, fn, code):
    try:
        fn()
    except (auth_core.AuthError, email_verify.MailError) as e:
        check(name, e.code == code, f"code={e.code}")
    else:
        check(name, False, "没报错")


print("— 票据 —")
t = email_verify.make_ticket("a@b.com")
check("票据有效", email_verify.verify_ticket("a@b.com", t))
check("票据不能换邮箱", not email_verify.verify_ticket("c@b.com", t))
check("篡改票据无效", not email_verify.verify_ticket("a@b.com", t[:-1] + ("0" if t[-1] != "0" else "1")))
check("空票据无效", not email_verify.verify_ticket("a@b.com", ""))
check("过期票据无效", not email_verify.verify_ticket("a@b.com", email_verify.make_ticket("a@b.com", ttl=-1)))
check("票据不认非邮箱", not email_verify.verify_ticket("随便", email_verify.make_ticket("a@b.com")))

print("— 验证码 —")


def put_code(email, code, tries=0, exp=None):
    salt = "s" * 16
    email_verify._CODES[email] = {
        "salt": salt,
        "hash": hashlib.sha256((salt + code).encode()).hexdigest(),
        "exp": exp if exp is not None else time.time() + 60,
        "tries": tries,
        "sent": time.time(),
    }


put_code("x@y.com", "123456")
tk = email_verify.check_code("x@y.com", "123456")
check("码正确换到票据", email_verify.verify_ticket("x@y.com", tk))
check("码一次性（用完即弃）", "x@y.com" not in email_verify._CODES)

put_code("x@y.com", "123456")
expect_err("错码被拒", lambda: email_verify.check_code("x@y.com", "000000"), "bad_code")
put_code("x@y.com", "123456", exp=time.time() - 1)
expect_err("过期码被拒", lambda: email_verify.check_code("x@y.com", "123456"), "code_expired")
put_code("x@y.com", "123456", tries=email_verify.CODE_TRIES)
expect_err("试太多次作废", lambda: email_verify.check_code("x@y.com", "123456"), "too_many_tries")
expect_err("没要过码就提交", lambda: email_verify.check_code("never@y.com", "123456"), "no_code")
expect_err("用户名当邮箱提交", lambda: email_verify.check_code("小明", "123456"), "bad_email")

print("— 没配 SMTP 时发码必须报 unconfigured（不能假装发了）—")
expect_err("未配发信时拒绝发码",
           lambda: email_verify.send_code("someone@example.com", "203.0.113.5"),
           "unconfigured")

print("— 注册闸门 —")
expect_err("用户名自助注册被拒",
           lambda: auth_core.register("随便一个名字", "secret123", ip="203.0.113.7"),
           "email_required")
expect_err("邮箱但没验码被拒",
           lambda: auth_core.register("new@x.com", "secret123", ip="203.0.113.8"),
           "email_unverified")
expect_err("拿别人的票据注册被拒",
           lambda: auth_core.register("new@x.com", "secret123", ip="203.0.113.8",
                                      email_ticket=email_verify.make_ticket("other@x.com")),
           "email_unverified")

u = auth_core.register("new@x.com", "secret123", ip="203.0.113.9",
                       email_ticket=email_verify.make_ticket("new@x.com"))
check("邮箱+票据注册成功", u["email"] == "new@x.com" and u["email_verified"] is True,
      f"verified={u['email_verified']}")

u2 = auth_core.register("BIG@X.com", "secret123", ip="203.0.113.10",
                        email_ticket=email_verify.make_ticket("big@x.com"))
check("邮箱大小写归一后仍认票据", u2["name"] == "big@x.com", u2["name"])

u3 = auth_core.register("ghuser", "secret123", ip="203.0.113.11",
                        github_id=999999, github_login="ghuser")
check("GitHub 路径照旧放行", u3["gh"] is True and u3["name"] == "ghuser")

u4 = auth_core.register("管理员", "secret123")
check("进程内建号（管理员/自检）不受限", u4["name"] == "管理员")
check("进程内建的邮箱号标未验证",
      auth_core.register("manual@x.com", "secret123")["email_verified"] is False)

print("— 登录不受影响 —")
check("邮箱号能登录", auth_core.login("new@x.com", "secret123")["id"] == u["id"])
check("大小写不敏感登录", auth_core.login("NEW@X.COM", "secret123")["id"] == u["id"])

print(f"\n通过 {len(OK)} / 失败 {len(BAD)}")
if BAD:
    print("失败项：" + "、".join(BAD))
sys.exit(1 if BAD else 0)
