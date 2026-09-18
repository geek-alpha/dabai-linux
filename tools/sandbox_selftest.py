"""沙箱与角色体系自检：不起服务，直接跑核心断言。

覆盖四层防线各自的证据：
  1) 角色与判重（auth_core）—— 第一个账号是管理员；同名变体必须被拒；
  2) 路径闸门（sandbox.resolve_path）—— 绝对越界 / ../ 越界 / 符号链接逃逸全拒；
  3) 工具策略（sandbox.tool_allowed）—— 管理员专属工具对普通用户不可见；
  4) 进程隔离（bwrap）—— 沙箱内看不到 /home/wxf，且写出的文件落在自己目录；
  5) 端到端（harness.execute_tool）—— 身份经 contextvar 穿过工具线程池后仍生效，
     这一条是回归防线：run_in_executor 不传播 context，漏了 copy_context
     工具线程里就是「系统身份」，整套沙箱静默失效。

用法：venv/bin/python tools/sandbox_selftest.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import auth_core  # noqa: E402
import sandbox  # noqa: E402

FAILS: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f"  | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    # ---------- 1. 角色与判重（用临时用户表，绝不碰真实 data/users.json） ----------
    section("角色与判重")
    auth_core.USERS_FILE = Path(tempfile.mkdtemp()) / "users.json"

    first = auth_core.register("王幸凤", "secret123", "selftest")
    check("第一个注册者自动成为管理员", first["role"] == "admin", str(first))
    second = auth_core.register("小明", "secret123")
    check("后续注册者是普通用户", second["role"] == "user", str(second))

    # 带空格的变体在名字格式检查（bad_name）就被拦下，不带空格的走判重（name_taken）——
    # 两条路都算「拒绝」，断言只看「有没有被拒」
    for variant in ("王幸凤", "王 幸凤", "王幸凤\u200b", "王幸凤 ", "　王幸凤"):
        try:
            auth_core.register(variant, "secret123")
            check(f"同名变体应被拒 {variant!r}", False)
        except auth_core.AuthError as e:
            check(f"同名变体被拒 {variant!r}", e.code in ("name_taken", "bad_name"), e.msg)

    auth_core.register("Alice", "secret123")
    for variant in ("alice", "Ａｌｉｃｅ", "ALICE"):
        try:
            auth_core.register(variant, "secret123")
            check(f"大小写/全角同名应被拒 {variant!r}", False)
        except auth_core.AuthError as e:
            check(f"大小写/全角同名被拒 {variant!r}", e.code == "name_taken")

    check("set_role 提升为管理员", auth_core.set_role(second["id"], "admin")["role"] == "admin")
    check("is_admin 生效", auth_core.is_admin(second["id"]))
    check("回收为普通用户", auth_core.set_role(second["id"], "user")["role"] == "user")
    check("未知角色被拒", _raises(lambda: auth_core.set_role(second["id"], "root")))
    check("登录返回带角色", auth_core.login("王幸凤", "secret123")["role"] == "admin")

    # ---------- 2. Actor 解析 ----------
    section("Actor")
    a_user = sandbox.actor_for(second["id"])
    a_admin = sandbox.actor_for(first["id"])
    check("普通用户不是管理员", not a_user.is_admin)
    check("普通用户沙箱在 data/sandboxes 下",
          str(a_user.sandbox).startswith(str(ROOT / "data" / "sandboxes")), str(a_user.sandbox))
    check("管理员是管理员", a_admin.is_admin)
    check("空 uid 按系统身份（局域网/CLI）", sandbox.actor_for("").is_admin)
    check("未知 uid 按系统身份（统一身份 default）", sandbox.actor_for("default").is_admin)

    # ---------- 3. 路径闸门 ----------
    section("路径闸门")
    check("绝对路径越界被拒", _raises(lambda: sandbox.resolve_path(a_user, "/etc/passwd")))
    check("../ 越界被拒", _raises(lambda: sandbox.resolve_path(a_user, "../../data/users.json")))
    check("相对路径落在沙箱内",
          str(sandbox.resolve_path(a_user, "a.txt")).startswith(str(a_user.sandbox)))
    esc = a_user.sandbox / "esc"
    try:
        if esc.is_symlink() or esc.exists():
            esc.unlink()
        esc.symlink_to("/etc")
        check("符号链接逃逸被拒", _raises(lambda: sandbox.resolve_path(a_user, "esc/passwd")))
        esc.unlink()
    except Exception as e:  # noqa: BLE001
        check("符号链接逃逸测试可执行", False, str(e))
    check("管理员读任意路径",
          str(sandbox.resolve_path(a_admin, "/etc/passwd")) == "/etc/passwd")

    # ---------- 4. 工具策略 ----------
    section("工具策略")
    for t in ("linux_process", "workspace_set", "wt_merge", "sched_add", "skill_pull_install",
              "delegate_agent_task", "harness_flow_submit", "code_verify", "read_web",
              "sys_find", "find_file", "system_check", "skill_dev_create", "mcp_call",
              "sub_agent_spawn", "show_screen_toast", "switch_character_model"):
        check(f"普通用户被拒：{t}", not sandbox.tool_allowed(a_user, t))
    for t in ("shell_run", "code_read", "code_edit", "code_create_file", "music_play",
              "weather_check", "image_gen_create", "search_web", "skill_help",
              "todo_create", "todo_list", "todo_update"):
        check(f"普通用户放行：{t}", sandbox.tool_allowed(a_user, t))
    check("管理员全部放行",
          all(sandbox.tool_allowed(a_admin, t) for t in
              ("linux_process", "workspace_set", "shell_run", "delegate_agent_task")))

    # ---------- 5. 参数改写 ----------
    section("参数改写")
    args = sandbox.prepare_args(a_user, "code_search", {"query": "x"})
    check("缺 root 的搜索工具注入沙箱目录", args.get("root") == str(a_user.sandbox), str(args))
    args2 = sandbox.prepare_args(a_user, "code_read", {"files": "a.py,b.py"})
    check("多路径逐项重写", str(a_user.sandbox) in args2["files"], args2["files"])
    check("参数越界被拒",
          _raises(lambda: sandbox.prepare_args(a_user, "code_read", {"files": "/etc/passwd"})))
    check("管理员参数原样不动",
          sandbox.prepare_args(a_admin, "code_read", {"files": "/etc/passwd"})
          == {"files": "/etc/passwd"})
    check("sandbox.py 自身语法可编译",
          _compiles(ROOT / "sandbox.py"))

    # ---------- 6. bwrap 进程隔离（真跑） ----------
    section("bwrap 进程隔离")
    if not sandbox.bwrap_available():
        check("bwrap 可用", False, "找不到 bwrap，普通用户将无法执行命令")
    else:
        target = a_user.sandbox / "inside.txt"
        if target.exists():
            target.unlink()
        argv, cwd = sandbox.wrap_shell(
            a_user,
            "echo hi > inside.txt; "
            "test -f /home/wxf/dabai/data/users.json && echo LEAK || echo ISOLATED; "
            "test -f /home/wxf/dabai/auth_core.py && echo LEAK2 || echo ISOLATED2",
            str(a_user.sandbox))
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        check("沙箱内命令可执行", r.returncode == 0, f"rc={r.returncode} out={out.strip()[:120]}")
        # 判据用文件级：test -d /home/wxf 恒为真（bwrap 为 bind 目标建了父目录链）
        check("沙箱内读不到服务器真实文件", "LEAK" not in out, out.strip()[:120])
        check("沙箱内写入落在自己目录", target.exists(), str(target))
        argv2, cwd2 = sandbox.wrap_shell(a_admin, "echo admin-ok", str(ROOT))
        check("管理员命令不加 bwrap", argv2[0] != sandbox.BWRAP and cwd2 == str(ROOT))

    # ---------- 7. 记忆与会话隔离 ----------
    section("记忆与会话隔离")
    asyncio.run(_memory_isolation())

    # ---------- 8. 注册闸门（开关 / 每 IP 限速 / 全局限速 / 人数上限） ----------
    section("注册闸门")
    auth_core.REG_OPEN_FILE = Path(tempfile.mkdtemp()) / "registration.json"
    auth_core._REG_HIST.clear()
    check("默认开放注册", auth_core.registration_open())

    ip = "203.0.113.7"
    for i in range(auth_core._REG_PER_IP):
        auth_core.register(f"闸门测试{i}", "secret123", ip=ip)
    check(f"同 IP 前 {auth_core._REG_PER_IP} 次注册成功",
          len(auth_core._REG_HIST[ip]) == auth_core._REG_PER_IP)
    try:
        auth_core.register("闸门测试超额", "secret123", ip=ip)
        check("同 IP 超限被拒", False)
    except auth_core.AuthError as e:
        check("同 IP 超限被拒", e.code == "rate_limited", e.msg)

    auth_core._REG_HIST.clear()
    keep_global = auth_core._REG_GLOBAL
    auth_core._REG_GLOBAL = 2
    try:
        auth_core.register("全局测试甲", "secret123", ip="198.51.100.1")
        auth_core.register("全局测试乙", "secret123", ip="198.51.100.2")
        try:
            auth_core.register("全局测试丙", "secret123", ip="198.51.100.3")
            check("换 IP 也撞全局限速", False)
        except auth_core.AuthError as e:
            check("换 IP 也撞全局限速", e.code == "rate_limited", e.msg)
    finally:
        auth_core._REG_GLOBAL = keep_global

    # 内部建号（管理员建号 / 自检）：不传 ip，不该被限速卡住
    auth_core._REG_HIST.clear()
    n_before = auth_core.user_count()
    for i in range(auth_core._REG_PER_IP + 1):
        auth_core.register(f"内部建号{i}", "secret123")
    check("进程内建号不受限速",
          auth_core.user_count() == n_before + auth_core._REG_PER_IP + 1)

    auth_core.REG_OPEN_FILE.write_text('{"open": false}', encoding="utf-8")
    check("开关关闭后 registration_open 为假", not auth_core.registration_open())
    try:
        auth_core.register("关闸后自助注册", "secret123", ip="203.0.113.9")
        check("关闸后自助注册被拒", False)
    except auth_core.AuthError as e:
        check("关闸后自助注册被拒", e.code == "registration_closed", e.msg)
    auth_core.register("关闸后内部建号", "secret123")
    check("关闸不影响内部建号", auth_core.find_by_name("关闸后内部建号") is not None)
    auth_core.REG_OPEN_FILE.unlink()

    keep_max = auth_core.MAX_USERS
    auth_core.MAX_USERS = auth_core.user_count()
    try:
        auth_core.register("超上限注册", "secret123")
        check("用户数达上限被拒", False)
    except auth_core.AuthError as e:
        check("用户数达上限被拒", e.code == "too_many_users", e.msg)
    finally:
        auth_core.MAX_USERS = keep_max
    auth_core._REG_HIST.clear()

    # ---------- 9. 端到端：身份穿过工具线程池 ----------
    section("端到端（harness 工具路径）")
    rc = asyncio.run(_e2e(a_user, a_admin))
    return 1 if (FAILS or rc) else 0


async def _memory_isolation() -> None:
    """记忆/会话必须按 user_id 隔离。

    这里守的是一个真实漏洞：get_or_create_session 原本按 namespace 取「所有身份里
    最近活跃」的会话并接管，普通用户一登录就会抢走管理员正在聊的会话（读到全部
    历史）；list_sessions 也不带 user 过滤。断言就是照着这两个点写的。
    """
    from memory import ChatMemory

    a = auth_core.register("隔离测试甲", "secret123")
    b = auth_core.register("隔离测试乙", "secret123")
    check("测试用户是普通用户", a["role"] == "user" and b["role"] == "user")

    ma = ChatMemory(user_id=a["id"], namespace="selftest_ns")
    mb = ChatMemory(user_id=b["id"], namespace="selftest_ns")
    await ma.create_new_session("甲会话")
    await mb.create_new_session("乙会话")
    check("两人拿到不同会话", ma.session_id != mb.session_id,
          f"{ma.session_id} vs {mb.session_id}")
    await ma.add_message("user", "甲的私密内容：松露巧克力")
    ids_a = {s["id"] for s in await ma.list_sessions()}
    ids_b = {s["id"] for s in await mb.list_sessions()}
    check("甲看得到自己的会话", ma.session_id in ids_a)
    check("甲看不到乙的会话", mb.session_id not in ids_a)
    check("乙看不到甲的会话", ma.session_id not in ids_b)
    check("归属校验：甲读不到乙的会话", not await ma.session_visible(mb.session_id))
    check("归属校验：甲读得到自己的会话", await ma.session_visible(ma.session_id))

    # 接管防线：新注册的普通用户不该接管任何人的活跃会话
    c = auth_core.register("隔离测试丙", "secret123")
    mc = ChatMemory(user_id=c["id"], namespace="selftest_ns")
    await mc.get_or_create_session()
    check("新用户不接管他人会话",
          mc.session_id not in (ma.session_id, mb.session_id), str(mc.session_id))
    check("新用户自己也看不到他人会话",
          not await mc.session_visible(ma.session_id))

    for m in (ma, mb, mc):
        try:
            await m.delete_session(m.session_id)
        except Exception:  # noqa: BLE001
            pass


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except sandbox.SandboxError:
        return True
    except auth_core.AuthError:
        return True
    except Exception:  # noqa: BLE001
        return False


def _compiles(path: Path) -> bool:
    import py_compile

    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _e2e(a_user, a_admin) -> int:
    from harness import get_harness

    h = get_harness()
    h.ensure_loaded()

    token = sandbox.push(a_user)
    try:
        text, src = await h.execute_tool(
            "shell_run",
            {"command": "test -f /home/wxf/dabai/data/users.json && echo LEAK || echo ISOLATED"})
        check("普通用户 shell_run 走沙箱",
              "[exit=0]\nISOLATED" in str(text), str(text)[:140])
        text2, _ = await h.execute_tool("linux_process", {"action": "list"})
        check("普通用户 linux_process 被拒", "仅管理员可用" in str(text2), str(text2)[:120])
        text3, _ = await h.execute_tool("code_read", {"files": "/etc/passwd"})
        check("普通用户读越界文件被拒", "沙箱" in str(text3) or "越界" in str(text3), str(text3)[:120])
    finally:
        sandbox.pop(token)

    token = sandbox.push(a_admin)
    try:
        text4, _ = await h.execute_tool("code_read", {"files": str(ROOT / "auth_core.py")})
        check("管理员读仓库文件正常", "def register" in str(text4), str(text4)[:80])
    finally:
        sandbox.pop(token)

    check("pop 之后回到系统身份", sandbox.current() is None)
    return 0


if __name__ == "__main__":
    code = main()
    print("\n" + ("全部通过 ✔" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    sys.exit(code)
