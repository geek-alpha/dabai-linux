"""按用户隔离的执行沙箱：非管理员用户只能在自己的目录里读写、跑命令。

为什么要有它（2026-09-15）：
    账号体系（auth_core.py）只解决了「谁能进来」，没解决「进来能碰什么」。
    在此之前任何注册用户发一句话就能让 agent 调 shell_run —— 工具实现层完全
    不知道调用者是谁，cwd 默认 os.getcwd() = 服务器仓库根。也就是说一个普通
    用户能读 data/users.json、data/auth_secret，能改服务器上的任何代码。

分层（纵深防御，缺一层都不算隔离）：
    1) 身份层 Actor：uid + role + 沙箱目录。agent 执行工具前 push 进 contextvar；
    2) 工具层 tool_allowed/check_tool：非管理员命中拒绝清单的工具直接拒绝（不执行）；
    3) 参数层 prepare_args：文件类工具的路径参数一律解析到沙箱内，越界拒绝；
       缺 root/dir 的搜索类工具注入沙箱目录，避免默认落到服务器仓库根；
    4) 进程层 wrap_shell：shell_run 换成 bwrap 包装 —— 只读挂载系统目录、单独
       挂载该用户沙箱、独立 /tmp、默认断网。实测沙箱内看不到 /home/wxf。

为什么是 bwrap 而不是 chroot/容器：
    本机 /usr/bin/bwrap 已存在，非 root 用户即可用（用户命名空间），零安装成本；
    docker 在 1GB 内存的树莓派上是负担。

身份默认值：current() 为 None 时按系统身份（等同管理员）放行 —— CLI、定时任务、
    局域网直连（unified_user_id）走的都是这条路，它们本来就有完整权限，收紧会把
    系统自己锁死。真正要防的是公网登录的普通用户，那条路径上 agent 一定会
    push 真实 actor（见 agent.py 的 _execute_tool）。
"""
from __future__ import annotations

import contextvars
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SANDBOX_DIR = DATA_DIR / "sandboxes"
SETTINGS_FILE = BASE_DIR / "settings.json"

ADMIN_ROLE = "admin"
USER_ROLE = "user"
SYSTEM_UID = "system"


class SandboxError(Exception):
    """沙箱越界/不可用。带中文原因，直接回填给模型。"""


@dataclass(frozen=True)
class Actor:
    """一次工具执行的执行者身份。"""

    uid: str
    role: str
    sandbox: Path

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE

    def describe(self) -> str:
        return f"{self.uid}({self.role})"


_current: contextvars.ContextVar = contextvars.ContextVar("dabai_actor", default=None)


def current() -> Optional[Actor]:
    """当前工具执行者；None = 系统身份（CLI/定时任务/局域网直连）。"""
    return _current.get()


def push(actor: Optional[Actor]):
    """设置当前执行者，返回 token（配合 pop 恢复，避免污染后续调用）。"""
    return _current.set(actor)


def pop(token) -> None:
    try:
        _current.reset(token)
    except Exception:
        pass


def system_actor() -> Actor:
    """系统身份：完整权限，沙箱目录指向 data/（与旧行为一致）。"""
    return Actor(uid=SYSTEM_UID, role=ADMIN_ROLE, sandbox=DATA_DIR)


def _user_role(uid: str) -> str:
    """从用户表取角色。取不到（旧用户没有 role 字段）一律按普通用户。"""
    try:
        import auth_core

        u = auth_core.get_user(uid)
        if not u:
            return ""
        return str(u.get("role") or USER_ROLE)
    except Exception:
        return ""


def actor_for(uid: str) -> Actor:
    """uid → Actor。空 uid 或不在用户表里的 uid（局域网统一身份）按系统身份。"""
    uid = str(uid or "").strip()
    if not uid:
        return system_actor()
    role = _user_role(uid)
    if not role:
        return system_actor()
    if role == ADMIN_ROLE:
        return Actor(uid=uid, role=ADMIN_ROLE, sandbox=DATA_DIR)
    return Actor(uid=uid, role=USER_ROLE, sandbox=ensure_sandbox(uid))


def ensure_sandbox(uid: str) -> Path:
    """该用户的沙箱目录（不存在则建）。目录名做安全化，杜绝 uid 里的路径穿越。"""
    safe = "".join(ch for ch in str(uid) if ch.isalnum() or ch in "_-")[:48] or "anon"
    p = SANDBOX_DIR / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def sandbox_root(actor: Optional[Actor]) -> Path:
    if actor is None or actor.is_admin:
        return Path.cwd()
    return actor.sandbox


# ---------- 路径闸门 ----------

def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_path(actor: Optional[Actor], raw: str, *, base: Optional[Path] = None) -> Path:
    """把工具参数里的路径解析成绝对路径；非管理员必须落在自己沙箱内。

    相对路径一律相对沙箱根（不是服务器 cwd）—— 否则 `code_read("agent.py")`
    会读到服务器源码。realpath 后再比较，符号链接指向外面同样被拒。
    """
    base = base or (Path.cwd() if actor is None or actor.is_admin else actor.sandbox)
    p = Path(str(raw).strip().strip('"').strip("'")).expanduser()
    if not p.is_absolute():
        p = base / p
    p = Path(os.path.normpath(str(p)))
    if actor is None or actor.is_admin:
        return p
    sb = Path(os.path.realpath(actor.sandbox))
    real = Path(os.path.realpath(p))
    if real != sb and not _inside(real, sb):
        raise SandboxError(
            f"越界：{raw} 不在你的沙箱内。你的可用目录是 {actor.sandbox}（相对路径即相对它解析）"
        )
    return p


# ---------- 工具策略 ----------

# 非管理员一律拒绝的工具（前缀匹配）：系统/进程/服务、工作区与工作树、委派与编排、
# 技能与插件安装、定时与长任务、跨用户管理、外观全局切换。
# 判据：这些工具的能力边界超出「一个用户在沙箱里自己玩」，或会改动全局状态影响别人。
_DENY_PREFIX = (
    "linux_", "workspace", "workspaces_", "wt_", "git_", "code_git_", "sys_",
    "sub_agent", "harness_", "skill_dev", "skill_pull", "mcp_", "sched_",
    "project_", "hanhua_", "anim_", "agent_profile_", "media_worker", "switch_",
    "android", "system_check", "hotmod_",
    # todo_ 已于 2026-09-16 对普通用户开放：待办库按用户分文件
    # （todo_impl 从沙箱身份层取 uid，落 data/users/<uid>/todo/tasks.json），
    # 不再有读到管理员经营信息的风险。
)

# 精确拒绝：无法用前缀覆盖，或必须单独说明理由的。
_DENY_EXACT = {
    "delegate_agent_task",   # 委派 = 以系统身份执行任意任务
    "list_agent_tasks",
    "show_screen_toast",     # 弹屏会打在管理员正看着的屏幕上
    "pmx_to_vrm",            # 写模型库全局目录
    "proxy_test", "fq_ctl",  # 全局网络出口
    "search_subdomains",     # 子域枚举，越权探测性质
    "read_web",              # 服务进程内抓任意 URL：能打 127.0.0.1 的本地接口
    "find_file",             # 全盘搜索，无路径参数可拦
    "code_verify", "code_smoke", "code_test", "code_review",  # 会执行任意代码
    "code_patch",            # 补丁可改多文件，闸门覆盖成本高于收益
}


def tool_allowed(actor: Optional[Actor], tool_name: str) -> bool:
    """该执行者能否使用这个工具（agent 用它过滤工具清单，避免模型白调一轮）。"""
    if actor is None or actor.is_admin:
        return True
    n = str(tool_name or "").strip()
    if not n:
        return False
    if n in _DENY_EXACT:
        return False
    return not n.startswith(_DENY_PREFIX)


def check_tool(actor: Optional[Actor], tool_name: str) -> Optional[str]:
    """执行前的兜底闸门。返回拒绝原因；None = 放行。"""
    if tool_allowed(actor, tool_name):
        return None
    return (
        f"工具 {tool_name} 仅管理员可用（你当前是普通用户，只能在自己的沙箱目录里工作）。"
        f"可用的沙箱目录：{actor.sandbox if actor else ''}"
    )


def filter_tools(actor: Optional[Actor], tools: list) -> list:
    """按执行者过滤工具定义列表（OpenAI function calling 格式）。"""
    if actor is None or actor.is_admin:
        return list(tools)
    out = []
    for t in tools:
        name = ((t.get("function") or {}) if isinstance(t, dict) else {}).get("name") or ""
        if tool_allowed(actor, name):
            out.append(t)
    return out


# 会被当成路径处理的参数名（各工具的命名实际就这么几种）。
_PATH_KEYS = ("path", "file", "files", "root", "dir", "dirs", "paths", "cwd", "work_dir")

# 这些工具缺 root/dir 时会退到服务器 cwd，必须注入沙箱目录。
_DEFAULT_ROOT_TOOLS = {
    "shell_run", "code_search", "code_list_files", "code_map", "code_analyze",
    "search_text", "list_files", "code_deps",
}


def _rewrite_value(actor: Actor, value):
    """路径参数值：字符串/列表/逗号分隔串统一逐项过闸门。"""
    if isinstance(value, (list, tuple)):
        return [_rewrite_value(actor, v) for v in value]
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value
    # 多路径惯用逗号或换行分隔（code_read 的 files、code_edit 的 files）
    sep = "," if ("," in raw and "\n" not in raw) else ("\n" if "\n" in raw else "")
    if sep:
        parts = [p for p in raw.split(sep) if p.strip()]
        return sep.join(str(resolve_path(actor, p)) for p in parts)
    return str(resolve_path(actor, raw))


def prepare_args(actor: Optional[Actor], tool_name: str, args: dict) -> dict:
    """执行前改写参数：路径全部落到沙箱内，缺省根目录注入沙箱。

    在参数校验（validate_arguments）之后调用，注入的 root 不会与工具 schema 冲突。
    """
    if actor is None or actor.is_admin or not isinstance(args, dict):
        return args
    out = dict(args)
    for k in _PATH_KEYS:
        if k in out and out[k] not in (None, "", [], ()):
            out[k] = _rewrite_value(actor, out[k])
    if tool_name in _DEFAULT_ROOT_TOOLS and not any(
        out.get(k) for k in ("root", "dir", "cwd")
    ):
        out["root"] = str(actor.sandbox)
    return out


# ---------- 进程层：bwrap ----------

BWRAP = shutil.which("bwrap")


def bwrap_available() -> bool:
    return bool(BWRAP)


def allow_net() -> bool:
    """沙箱是否放通网络：settings.json -> sandbox.allow_net，默认 false。"""
    try:
        cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return bool(((cfg or {}).get("sandbox") or {}).get("allow_net", False))
    except Exception:
        return False


def wrap_shell(actor: Actor, command: str, cwd: str = "") -> tuple:
    """非管理员的 shell 命令 → bwrap argv；管理员原样返回。

    返回 (argv, cwd)：argv 为列表时走 exec 直传（不经过 shell 二次解析）。

    验证隔离时的坑：bind 目标用的是宿主绝对路径，bwrap 会为它自动创建父目录链——
    所以沙箱内 `ls /home/wxf` 会看到一个**空的** dabai 目录，而不是真实内容。
    判据不能用 `test -d /home/wxf`（恒为真），要用文件级：
    `test -f /home/wxf/dabai/data/users.json`。

    不用 --new-session：它只改会话归属，隔离不靠它，但会让超时后的进程难以回收。
    """
    work = Path(cwd) if cwd else actor.sandbox
    if actor.is_admin:
        return ["/bin/sh", "-c", command], str(work)
    if not BWRAP:
        raise SandboxError("沙箱不可用（找不到 bwrap），普通用户不能执行命令")
    sb = str(actor.sandbox)
    argv = [
        BWRAP,
        "--die-with-parent",
        # 只读系统目录：给解释器/编译器等运行环境，但不挂 /home、不挂仓库
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", sb, sb,
        "--chdir", str(work),
    ]
    if not allow_net():
        argv.append("--unshare-net")
    argv += ["--", "/bin/sh", "-c", command]
    return argv, str(work)
