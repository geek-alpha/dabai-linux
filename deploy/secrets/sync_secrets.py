#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DABAI 密钥同步器 —— 把散落在 JSON 配置里的 API Key 汇聚成一份系统环境变量文件。

架构（单向派生，避免双真源）：

    真源   settings.json / codex_config.json / stt_config.json   ← UI 改这里
      │
      │  sync（本脚本；JSON 一变就重新生成）
      ▼
    派生   /etc/dabai/secrets.env   root:wxf 0640                 ← 不手改，会被覆盖
      │
      ├── systemd  EnvironmentFile=       → myservice 及其全部子进程
      └── login    /etc/profile.d/*.sh    → wxf 的交互式 shell

设计要点：

1. **只维护标记块**。文件里 BEGIN/END 之间的内容由本脚本生成，块外内容原样保留。
   因此 EXA_API_KEY / TAVILY_API_KEY / GITHUB_TOKEN 这类「纯环境变量型」密钥
   可以和派生变量共存，互不干扰。

2. **幂等**。内容无变化时不写盘 —— 否则 systemd path unit 会被自己触发成死循环。

3. **不用 `Environment=`**。systemd 的 `Environment=` 内容会被 `systemctl show`
   明文暴露给任何用户（已实测）；`EnvironmentFile=` 只暴露路径，不暴露内容。

4. **值一律单引号包裹**且做字符集校验。systemd EnvironmentFile 与 POSIX shell
   对转义的处理规则不同，唯一双方都安全的是「不含单引号的安全字符集」。
   遇到越界字符直接拒绝并报错，绝不猜。

5. **变量名白名单**。CLI 的 set 拒绝 PATH / LD_PRELOAD / IFS 之类，
   防止把密钥文件变成提权跳板。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1.0.0"

# ---------- 路径 ----------

TARGET = Path(os.environ.get("DABAI_SECRETS_FILE") or "/etc/dabai/secrets.env")
TARGET_DIR = TARGET.parent
TARGET_MODE = 0o640
TARGET_DIR_MODE = 0o750
TARGET_GROUP = os.environ.get("DABAI_SECRETS_GROUP") or "wxf"

BEGIN = "# >>> DABAI MANAGED >>> 由 dabai-secrets sync 自动生成；手改会在下次同步被覆盖"
END = "# <<< DABAI MANAGED <<<"

# 手工维护区提示（块外，永不触碰）
HANDWRITTEN_HINT = "# 本行以下为手工变量区（同步器不碰）"

# ---------- 安全约束 ----------

# 允许通过 CLI 写入的变量名前缀（拒绝 PATH/LD_*/IFS 等危险名）
ALLOW_PREFIX = (
    "DABAI_", "EXA_", "TAVILY_", "GITHUB_", "OPENAI_", "ANTHROPIC_",
    "HUGGINGFACE_", "HF_", "SILICONFLOW_", "DEEPSEEK_", "MOONSHOT_",
)
# 永不接受的变量名（哪怕前缀碰巧匹配）
DENY_NAMES = {
    "PATH", "IFS", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "OLDPWD",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP",
    "BASH_ENV", "ENV", "TMPDIR", "SUDO_ASKPASS", "SSH_AUTH_SOCK",
}

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# systemd + POSIX shell 双方都无需转义的字符集
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=:@%\-]*$")

SOURCES = ("settings.json", "codex_config.json", "stt_config.json")

# ---------- 源文件快照（防误删 / 改坏）----------

# settings.json / nodes.json 这类文件既含密钥又未被 git 跟踪 —— git 救不了它们。
# 实测事故：一条 `>` 重定向覆盖 + 一次 rm，settings.json 就彻底没了。
# 所以每次同步顺带做一份滚动快照。
SNAPSHOT_DIR = Path(os.environ.get("DABAI_SNAPSHOT_DIR") or "/var/backups/dabai-configs")
SNAPSHOT_KEEP = 20
SNAPSHOT_STATE = Path(
    os.environ.get("DABAI_SNAPSHOT_STATE") or "/var/lib/dabai-configs-snapshot.sha256"
)
SNAPSHOT_FILES = (
    "settings.json", "codex_config.json", "stt_config.json", "tts_config.json",
    "cards.json", "character_cards.json", "nodes.json",
)
# 只认自己建的目录（清理时不会被别的目录干扰）
SNAPSHOT_NAME_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class SecretError(Exception):
    """密钥文件层面的错误（非预期状态，需人工处理）。"""


# ---------- 仓库定位 ----------


def find_repo() -> Path:
    """定位仓库根目录（settings.json + server.py 所在处）。"""
    env = (os.environ.get("DABAI_REPO") or "").strip()
    if env and (Path(env) / "settings.json").is_file():
        return Path(env).resolve()

    here = Path(__file__).resolve()
    for cand in [here.parent, *here.parents]:
        if (cand / "settings.json").is_file() and (cand / "server.py").is_file():
            return cand

    for cand in (Path("/home/wxf/dabai"), Path("/opt/dabai"), Path.home() / "dabai"):
        if (cand / "settings.json").is_file():
            return cand

    raise SecretError(
        "找不到仓库根目录（需含 settings.json）；可用 DABAI_REPO=/path 指定"
    )


# ---------- 采集 ----------


def _slug(raw: str) -> str:
    """prov-5cd04264 → 5CD04264；prov-ollama → OLLAMA。用于变量名，必须稳定。"""
    s = re.sub(r"^prov-", "", str(raw or "").strip(), flags=re.I)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return s or "UNKNOWN"


def _load_json(path: Path, warnings: list) -> dict | None:
    if not path.is_file():
        warnings.append(f"{path.name} 不存在，跳过")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # 配置坏了不能连累同步
        warnings.append(f"{path.name} 解析失败：{exc}")
        return None
    if not isinstance(data, dict):
        warnings.append(f"{path.name} 顶层不是对象，跳过")
        return None
    return data


def collect(repo: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """采集全部密钥。

    返回 (items, warnings)，items 为有序 [(变量名, 值, 说明)]。
    值会做 strip；空值一律跳过（不生成空变量，避免下游误判「已配置」）。
    """
    items: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}

    def add(var: str, val, note: str) -> None:
        v = str(val or "").strip()
        if not v:
            return
        if not NAME_RE.match(var):
            warnings.append(f"变量名非法，跳过：{var}")
            return
        if var in seen:
            # 同名冲突：保留先出现的（顶层优先于供应商），并提示
            warnings.append(f"{var} 重复（{seen[var]} vs {note}），保留前者")
            return
        seen[var] = note
        items.append((var, v, note))

    # ---- settings.json ----
    cfg = _load_json(repo / "settings.json", warnings)
    if cfg:
        add("DABAI_API_KEY", cfg.get("api_key"), "settings.json 顶层主 Key")
        add("DABAI_BASE_URL", cfg.get("base_url"), "settings.json 顶层地址")
        add("DABAI_MODEL", cfg.get("model"), "settings.json 顶层模型")
        add("DABAI_IMAGES_API_KEY", cfg.get("images_api_key"), "settings.json 图像生成")
        add("DABAI_IMAGES_BASE_URL", cfg.get("images_base_url"), "settings.json 图像接口")

        profiles = cfg.get("llm_profiles")
        if isinstance(profiles, dict):
            for key, prof in profiles.items():
                if isinstance(prof, dict):
                    add(
                        f"DABAI_PROFILE_{_slug(key)}_API_KEY",
                        prof.get("api_key"),
                        f"settings.json llm_profiles.{key}（旧格式）",
                    )

        providers = cfg.get("llm_providers")
        if isinstance(providers, list):
            for p in providers:
                if not isinstance(p, dict):
                    continue
                sl = _slug(p.get("id"))
                name = str(p.get("name") or sl).strip()
                add(f"DABAI_PROV_{sl}_API_KEY", p.get("api_key"), f"供应商「{name}」")
                add(f"DABAI_PROV_{sl}_BASE_URL", p.get("base_url"), f"供应商「{name}」地址")
                add(f"DABAI_PROV_{sl}_MODEL", p.get("default_model"), f"供应商「{name}」默认模型")

            # 当前激活供应商 → 无歧义别名
            active_id = str(cfg.get("llm_provider_id") or "").strip()
            active = None
            for p in providers:
                if isinstance(p, dict) and active_id and p.get("id") == active_id:
                    active = p
                    break
            if active is None:
                kind = str(cfg.get("llm_provider") or "").strip()
                for p in providers:
                    if isinstance(p, dict) and kind and p.get("kind") == kind:
                        active = p
                        break
            if active is None:
                for p in providers:
                    if isinstance(p, dict) and str(p.get("base_url") or "").strip():
                        active = p
                        break
            if isinstance(active, dict):
                nm = str(active.get("name") or "").strip()
                add("DABAI_ACTIVE_API_KEY", active.get("api_key"), f"当前激活供应商「{nm}」")
                add("DABAI_ACTIVE_BASE_URL", active.get("base_url"), f"当前激活供应商「{nm}」地址")
                add("DABAI_ACTIVE_MODEL", active.get("default_model"), f"当前激活供应商「{nm}」模型")

    # ---- codex_config.json ----
    cdx = _load_json(repo / "codex_config.json", warnings)
    if cdx:
        llm = cdx.get("llm") if isinstance(cdx.get("llm"), dict) else {}
        add("DABAI_CODEX_API_KEY", llm.get("api_key"), "codex_config.json llm")
        add("DABAI_CODEX_BASE_URL", llm.get("base_url"), "codex_config.json llm 地址")
        add("DABAI_CODEX_MODEL", llm.get("model"), "codex_config.json llm 模型")

    # ---- stt_config.json ----
    stt = _load_json(repo / "stt_config.json", warnings)
    if stt:
        add("DABAI_STT_API_KEY", stt.get("api_key"), "stt_config.json 语音识别")

    # ---- tts_config.json（当前为空，但保持接口） ----
    tts = _load_json(repo / "tts_config.json", warnings)
    if tts:
        add("DABAI_TTS_API_KEY", tts.get("api_key"), "tts_config.json 语音合成")

    # 去掉「文件不存在」这类噪音警告（正常情况）
    warnings = [w for w in warnings if "不存在，跳过" not in w]
    return items, warnings


# ---------- 渲染与写入 ----------


def _quote(value: str) -> str:
    """把值渲染成 systemd EnvironmentFile 与 POSIX shell 双兼容的字面量。

    只接受安全字符集；含单引号/换行/反斜杠等一律拒绝 —— 两个解析器对转义的
    规则不一致，硬转义就是在赌，宁可报错。
    """
    if not SAFE_VALUE_RE.match(value):
        bad = sorted({c for c in value if not re.match(r"[A-Za-z0-9._~+/=:@%\-]", c)})
        raise SecretError(
            f"值含不安全字符 {bad}，拒绝写入（systemd 与 shell 的转义规则不同，不做猜测）"
        )
    return f"'{value}'"


def render_block(items: list[tuple[str, str, str]], repo: Path, *, masked: bool = False) -> str:
    lines = [BEGIN]
    lines.append(f"# 生成源：{repo}")
    lines.append(f"# 生成器：dabai-secrets {VERSION}（sync_secrets.py）")
    lines.append("# 变量名规则：DABAI_PROV_<供应商ID>_API_KEY / DABAI_ACTIVE_* / DABAI_<域>_API_KEY")
    lines.append("#")
    for var, val, note in items:
        lines.append(f"# {note}")
        # masked 仅供 dry-run 预览：绝不在终端回显明文
        lines.append(f"{var}={'*' * 12 if masked else _quote(val)}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def split_managed(text: str) -> tuple[str, str, str]:
    """把现有文件拆成 (块前, 块, 块后)。

    没有标记块时，整份内容视为「块前」，块为空 —— 首次同步不会吃掉已有手工变量。
    """
    bi = text.find(BEGIN)
    if bi < 0:
        return text, "", ""
    ei = text.find(END, bi)
    if ei < 0:
        # 有头无尾：当成损坏，整份内容作块前，避免误删
        return text, "", ""
    ei_end = ei + len(END)
    # 吃掉块尾换行
    if text[ei_end:ei_end + 1] == "\n":
        ei_end += 1
    return text[:bi], text[bi:ei_end], text[ei_end:]


def compose(existing: str, new_block: str) -> str:
    """把新块嵌入现有内容，保留块外一切（含手工变量）。"""
    before, _old_block, after = split_managed(existing)

    if not existing.strip():
        # 空文件：块 + 手工区骨架
        return (
            "# /etc/dabai/secrets.env —— DABAI 系统环境变量（自动生成 + 手工变量共存）\n"
            "# 权限 root:wxf 0640；systemd 经 EnvironmentFile= 注入，登录 shell 经 profile.d 加载。\n"
            "# 手工变量请写在下方 MANAGED 块之外，同步器不会碰。\n"
            "\n"
            + new_block
            + "\n"
            + HANDWRITTEN_HINT
            + "\n"
        )

    head = before.rstrip("\n")
    tail = after
    out = (head + "\n\n" if head else "") + new_block
    if tail.strip():
        out += "\n" + tail.lstrip("\n")
    elif HANDWRITTEN_HINT not in out:
        out += "\n" + HANDWRITTEN_HINT + "\n"
    return out


def read_target() -> str:
    try:
        return TARGET.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except PermissionError as exc:
        raise SecretError(
            f"无权读取 {TARGET}（需 root 或 wxf 组）：{exc}\n"
            f"  提示：sudo -n /usr/local/sbin/dabai-secrets sync"
        ) from exc


def atomic_write(content: str) -> None:
    """原子写 + 立刻落权限，避免「先写后 chmod」的窗口期泄漏。"""
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(TARGET_DIR), prefix=".secrets.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, TARGET_MODE)
        try:
            import grp

            gid = grp.getgrnam(TARGET_GROUP).gr_gid
            os.chown(tmp, 0, gid)
        except (KeyError, PermissionError, ImportError):
            # 组不存在或无权限改属主：保持当前属主，仅靠 0640 保护
            pass
        os.replace(tmp, TARGET)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_perms() -> list[str]:
    """摆正目标**文件**的权限与属主。

    刻意不调整目录权限 —— 目标路径可由 DABAI_SECRETS_FILE 指向任意位置
    （测试、多实例、非 /etc），无条件 chmod 会把别人的目录改坏
    （实测：隔离测试时它试图 chmod /tmp 而被拒）。目录权限由
    install-secrets.sh 在创建 /etc/dabai 时一次性设定。

    全部操作 best-effort：失败只记录，绝不中断同步 —— 密钥写进去比权限完美
    更重要，权限问题由 check 子命令报出来。
    """
    notes: list[str] = []
    if not TARGET.is_file():
        return notes

    try:
        import grp

        gid = grp.getgrnam(TARGET_GROUP).gr_gid
    except (KeyError, ImportError):
        gid = None

    try:
        cur = TARGET.stat()
        if cur.st_mode & 0o777 != TARGET_MODE:
            os.chmod(TARGET, TARGET_MODE)
            notes.append(f"文件权限 → {oct(TARGET_MODE)}")
        if gid is not None and cur.st_gid != gid and os.geteuid() == 0:
            os.chown(TARGET, 0, gid)
            notes.append(f"文件属组 → {TARGET_GROUP}")
    except OSError as exc:
        notes.append(f"权限调整失败（不影响密钥写入）：{exc}")
    return notes


def snapshot_sources(repo: Path, *, quiet: bool = False) -> dict:
    """把源配置快照进 SNAPSHOT_DIR；内容没变就不落盘。

    只在「内容或文件集合发生变化」时存一份，保留最近 SNAPSHOT_KEEP 份，
    目录 0700 / 文件 0600。失败一律不抛异常 —— 快照是兜底，
    绝不能因为它坏了而连累主同步链。
    """
    files: dict[str, bytes] = {}
    for name in SNAPSHOT_FILES:
        p = repo / name
        try:
            if p.is_file():
                files[name] = p.read_bytes()
        except OSError:
            pass
    if not files:
        return {"saved": False, "reason": "无源文件可快照"}

    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(files[name])
    cur = digest.hexdigest()

    try:
        if SNAPSHOT_STATE.is_file() and SNAPSHOT_STATE.read_text().strip() == cur:
            return {"saved": False, "reason": "内容未变"}
    except OSError:
        pass

    # 已有同内容的快照 → 不重复存（否则「内容改回去」会把滚动窗口撑满重复份）
    try:
        if SNAPSHOT_DIR.is_dir():
            for d in SNAPSHOT_DIR.iterdir():
                if (d.is_dir() and SNAPSHOT_NAME_RE.match(d.name)
                        and d.name.endswith(cur[:8])):
                    try:
                        SNAPSHOT_STATE.parent.mkdir(parents=True, exist_ok=True)
                        SNAPSHOT_STATE.write_text(cur, encoding="utf-8")
                    except OSError:
                        pass
                    return {"saved": False, "reason": "同内容快照已存在", "dir": str(d)}
    except OSError:
        pass

    # 名字带内容摘要：同一秒内多次变化不会互相覆盖，内容相同则天然去重
    dest = SNAPSHOT_DIR / (time.strftime("%Y%m%d-%H%M%S") + "-" + cur[:8])
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SNAPSHOT_DIR, 0o700)
        dest.mkdir(mode=0o700, exist_ok=True)
        for name, data in files.items():
            f = dest / name
            f.write_bytes(data)
            os.chmod(f, 0o600)
        SNAPSHOT_STATE.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_STATE.write_text(cur, encoding="utf-8")
    except OSError as exc:
        return {"saved": False, "reason": f"写入失败：{exc}"}

    removed: list[str] = []
    try:
        snaps = sorted(
            d for d in SNAPSHOT_DIR.iterdir()
            if d.is_dir() and SNAPSHOT_NAME_RE.match(d.name)
        )
        for old in snaps[:-SNAPSHOT_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
            removed.append(old.name)
    except OSError:
        pass

    if not quiet:
        print(f"✓ 配置快照 → {dest}（{len(files)} 个文件；保留最近 {SNAPSHOT_KEEP} 份）")
    return {"saved": True, "dir": str(dest), "count": len(files), "removed": removed}


def sync(repo: Path, *, dry_run: bool = False, quiet: bool = False) -> dict:
    """核心同步：采集 → 渲染 → 与现有内容比对 → 仅在变化时原子写。"""
    snap = snapshot_sources(repo, quiet=quiet)
    items, warnings = collect(repo)
    new_block = render_block(items, repo)
    existing = read_target() if TARGET.is_file() else ""
    desired = compose(existing, new_block)

    changed = desired != existing
    result = {
        "changed": changed,
        "count": len(items),
        "items": items,
        "warnings": warnings,
        "target": str(TARGET),
        "snapshot": snap,
    }

    if dry_run:
        result["preview"] = desired
        return result

    if not changed:
        result["perms"] = apply_perms()
        return result

    atomic_write(desired)
    result["perms"] = apply_perms()
    if not quiet:
        print(f"✓ 已同步 {len(items)} 个变量 → {TARGET}")
    return result


def parse_env_file(text: str) -> dict[str, str]:
    """解析 env 文件为 {name: value}（只做保守解析：KEY='...' 或 KEY=...）。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip()
        if not NAME_RE.match(name):
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[name] = val
    return out


# ---------- 展示辅助 ----------


def mask(value: str) -> str:
    """脱敏：保留前 6 位（判断是哪家的 key）+ 长度，中间打码。"""
    v = str(value or "")
    if not v:
        return "(空)"
    if len(v) <= 8:
        return "*" * len(v) + f"  (len={len(v)})"
    return f"{v[:6]}…{v[-2:]}  (len={len(v)})"


def require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise SecretError(
            f"{action} 需要 root（要写 {TARGET}）。\n"
            f"  请用：sudo -n /usr/local/sbin/dabai-secrets {action}\n"
            f"  若未安装特权副本：sudo bash deploy/secrets/install-secrets.sh"
        )


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise SecretError(f"变量名非法：{name!r}（只允许大写字母/数字/下划线，且不以数字开头）")
    if name in DENY_NAMES:
        raise SecretError(f"拒绝设置危险变量名：{name}（会影响进程行为，属于提权风险）")
    if not name.startswith(ALLOW_PREFIX):
        raise SecretError(
            f"拒绝设置 {name}：只允许这些前缀 {', '.join(ALLOW_PREFIX)}\n"
            f"  （防止把密钥文件变成注入任意环境变量的跳板）"
        )


# ---------- 子命令 ----------


def _assert_writable() -> None:
    """写入前预检权限，把「Permission denied 堆栈」换成可执行的提示。"""
    probe = TARGET_DIR if TARGET_DIR.exists() else TARGET_DIR.parent
    if not os.access(probe, os.W_OK):
        raise SecretError(
            f"需要 root 才能写 {TARGET}（当前用户对 {probe} 无写权限）。\n"
            f"  请用：sudo -n /usr/local/sbin/dabai-secrets sync\n"
            f"  若未安装特权副本：sudo bash deploy/secrets/install-secrets.sh"
        )


def cmd_sync(args) -> int:
    repo = find_repo()
    if not args.dry_run:
        _assert_writable()
    res = sync(repo, dry_run=args.dry_run, quiet=args.quiet)

    for w in res["warnings"]:
        print(f"  ! {w}", file=sys.stderr)

    if args.dry_run:
        items, _ = collect(repo)
        print(render_block(items, repo, masked=True))
        print(f"[dry-run] 将写入 {res['count']} 个变量 → {res['target']}（值已脱敏）")
        return 0

    if res["changed"]:
        print(f"✓ 同步完成：{res['count']} 个变量 → {res['target']}")
    else:
        print(f"= 无变化（{res['count']} 个变量已是最新）")
    for note in res.get("perms") or []:
        print(f"  · 权限修正：{note}")
    return 0


def cmd_list(args) -> int:
    repo = find_repo()
    items, warnings = collect(repo)
    derived = {v for v, _, _ in items}

    print(f"派生变量（来自 JSON 配置，{len(items)} 个）—— {repo}")
    for var, val, note in items:
        print(f"  {var:38} {mask(val):24} {note}")

    manual: dict[str, str] = {}
    if TARGET.is_file():
        try:
            manual = {
                k: v for k, v in parse_env_file(read_target()).items()
                if k not in derived
            }
        except SecretError as exc:
            print(f"\n! 读取 {TARGET} 失败：{exc}", file=sys.stderr)

    if manual:
        print(f"\n手工变量（不在 JSON 里，同步器不碰，{len(manual)} 个）")
        for var, val in sorted(manual.items()):
            print(f"  {var:38} {mask(val)}")

    if not TARGET.is_file():
        print(f"\n! {TARGET} 尚不存在 —— 先跑 sync（需 root）")

    for w in warnings:
        print(f"\n! {w}", file=sys.stderr)
    return 0


def cmd_check(args) -> int:
    """检查：派生变量与磁盘内容是否一致、权限是否正确。返回码 1 = 有问题。"""
    repo = find_repo()
    items, warnings = collect(repo)
    problems: list[str] = []

    if not TARGET.is_file():
        problems.append(f"{TARGET} 不存在")
    else:
        existing = read_target()
        on_disk = parse_env_file(existing)
        for var, val, _ in items:
            if var not in on_disk:
                problems.append(f"{var} 缺失")
            elif on_disk[var] != val:
                problems.append(f"{var} 与 JSON 不一致（磁盘 {mask(on_disk[var])} vs 配置 {mask(val)}）")

        desired = compose(existing, render_block(items, repo))
        if desired != existing:
            problems.append("文件内容与当前配置不同步（跑一次 sync 即可）")

        st = TARGET.stat()
        if st.st_mode & 0o777 != TARGET_MODE:
            problems.append(f"{TARGET} 权限为 {oct(st.st_mode & 0o777)}，应为 {oct(TARGET_MODE)}")
        try:
            import grp

            want_gid = grp.getgrnam(TARGET_GROUP).gr_gid
            if st.st_gid != want_gid:
                problems.append(f"{TARGET} 属组不是 {TARGET_GROUP}")
        except (KeyError, ImportError):
            pass

    print(f"配置源：{repo}")
    print(f"目标文件：{TARGET}")
    print(f"派生变量：{len(items)} 个")
    for w in warnings:
        print(f"  ! {w}")

    if problems:
        print(f"\n✗ 发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n✓ 全部一致")
    return 0


def cmd_show(args) -> int:
    repo = find_repo()
    items, _ = collect(repo)
    table = {v: val for v, val, _ in items}
    if TARGET.is_file():
        for k, v in parse_env_file(read_target()).items():
            table.setdefault(k, v)

    name = args.name
    if name not in table:
        print(f"✗ 没有这个变量：{name}", file=sys.stderr)
        return 1
    if args.reveal:
        print(table[name])
    else:
        print(f"{name} = {mask(table[name])}")
        print("（加 --reveal 显示明文）")
    return 0


def _mutate(name: str, value: str | None) -> int:
    """在手工区增删改变量（需 root）。派生变量只读，不允许在此改。"""
    require_root("set")
    check_name(name)

    repo = find_repo()
    items, _ = collect(repo)
    if name in {v for v, _, _ in items}:
        raise SecretError(
            f"{name} 是派生变量（来自 JSON 配置），不能在这里改。\n"
            f"  请改对应 JSON，或直接编辑 settings.json 后会自动同步。"
        )

    existing = read_target() if TARGET.is_file() else ""
    before, _blk, after = split_managed(existing)

    lines = []
    found = False
    for raw in after.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            lines.append(raw)
            continue
        nm = stripped.split("=", 1)[0].strip()
        if nm == name:
            found = True
            if value is None:
                continue  # 删除
            lines.append(f"{name}={_quote(value)}")
        else:
            lines.append(raw)

    if value is not None and not found:
        lines.append(f"{name}={_quote(value)}")

    tail = "\n".join(lines)
    if tail.strip() and HANDWRITTEN_HINT not in tail:
        tail = HANDWRITTEN_HINT + "\n" + tail
    new_text = before + (existing[len(before):len(existing) - len(after)] if after else "") + tail
    if not new_text.endswith("\n"):
        new_text += "\n"

    # 保持 MANAGED 块存在
    if BEGIN not in new_text:
        new_text = render_block(items, repo) + "\n" + new_text

    atomic_write(new_text)
    apply_perms()
    print(f"✓ {'已设置' if value is not None else '已删除'} {name}")
    return 0


def cmd_set(args) -> int:
    return _mutate(args.name, args.value)


def cmd_unset(args) -> int:
    return _mutate(args.name, None)


def cmd_env(args) -> int:
    """输出可直接 eval/source 的 shell 片段（供脚本内联使用）。"""
    repo = find_repo()
    items, _ = collect(repo)
    if not TARGET.is_file():
        print(f"✗ {TARGET} 不存在，先 sync", file=sys.stderr)
        return 1
    table = parse_env_file(read_target())
    for var, _, _ in items:
        if var in table:
            print(f"export {var}={_quote(table[var])}")
    if args.include_manual:
        derived = {v for v, _, _ in items}
        for var, val in sorted(table.items()):
            if var not in derived and SAFE_VALUE_RE.match(val):
                print(f"export {var}='{val}'")
    return 0


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dabai-secrets",
        description="DABAI 密钥同步器：JSON 配置 → 系统环境变量文件（并实时同步）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用：\n"
            "  dabai-secrets sync          从 JSON 重新生成环境变量文件（需 root）\n"
            "  dabai-secrets list          列出全部变量（默认脱敏）\n"
            "  dabai-secrets check         校验一致性，不一致返回码 1\n"
            "  dabai-secrets show NAME     看单个变量（--reveal 显示明文）\n"
            "  dabai-secrets set NAME VAL  设置手工变量（需 root，名有白名单）\n"
            "  dabai-secrets env           输出 export 语句，供脚本 source\n"
            "\n"
            "实时同步：systemd path unit 监听 3 个 JSON，改动即触发 sync（见 install-secrets.sh）\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"dabai-secrets {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", help="从 JSON 重新生成（需 root）")
    s.add_argument("--dry-run", action="store_true", help="只打印将写入的内容，不落盘")
    s.add_argument("--quiet", action="store_true", help="静默（供 systemd 调用）")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("list", help="列出全部变量")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("check", help="校验一致性")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("show", help="显示单个变量")
    s.add_argument("name")
    s.add_argument("--reveal", action="store_true", help="显示明文（默认脱敏）")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("set", help="设置手工变量（需 root）")
    s.add_argument("name")
    s.add_argument("value")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("unset", help="删除手工变量（需 root）")
    s.add_argument("name")
    s.set_defaults(func=cmd_unset)

    s = sub.add_parser("env", help="输出 export 语句")
    s.add_argument("--include-manual", action="store_true", help="连同手工变量一起输出")
    s.set_defaults(func=cmd_env)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["sync"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except SecretError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
