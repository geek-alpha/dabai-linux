#!/usr/bin/env python3
"""节点侧自动更新器 —— 把新版本装上，同时保证「经历」一个字节都不会被动到。

硬线只有一条：
    更新器只写「清单 ∩ 代码档」的交集，其余一律不碰。
而「代码档」的判定**不来自清单**。清单是发布方给的，可能被改坏、可能被投毒；
判定是更新器自带的、冻结的。两个独立来源取交集，任一方出问题都到不了经历文件。

为什么这个文件不 import 仓库里的 paths.py / manifest.py：
    仓库正是被更新的对象。更新器一旦依赖仓库内文件，就出现「用待更新的代码
    去校验更新是否安全」的循环 —— 包被换掉时，校验器也一起被换掉了。
    所以这里自带一份最小的判定与清单读取，宁可重复，不可依赖。
    测试 test_floor_matches_paths 断言内嵌地板与 paths.py 不漂移。

流程：
    取版本 → 下载 → 校验包哈希 → 解包暂存 → 校验清单与逐文件 sha256
    → 用自带地板复核清单（出现受保护路径 = 整包作废，不是跳过那一条）
    → 停机 → 记录经历文件快照 → 逐文件原子替换（旧版留备份）
    → 复核经历文件快照 → 起服务 → 健康检查 → 失败自动回滚

用法：
    python update.py --check                     # 只看有没有新版
    python update.py --dry-run                   # 全流程演练，不写盘不重启
    python update.py --apply                     # 真更新
    python update.py --apply --local-tarball X --local-manifest Y   # 离线/测试
    python update.py --rollback                  # 回滚到上一版
    python update.py --apply --tag v1.0.0        # 切到指定版本（可降级）
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
MANIFEST_NAME = "MANIFEST.json"

# ── 配置默认值（可被 /etc/dabai/update.conf 或命令行覆盖）──────────────────
DEFAULTS = {
    "ROOT": "/home/wxf/dabai",
    "STATE": "/var/lib/dabai-update",
    "REPO": "wangxingfen/dabai-linux",
    "SERVICE": "myservice",
    "PORT": "8000",
    "ENTRY": "server.py",
    "KEEP_BACKUPS": "3",
    "HTTP_TIMEOUT": "60",
}

# ── 保护地板（冻结）──────────────────────────────────────────────────────
# 与 deploy/release/paths.py 的 FLOOR_GLOBS 同源；测试断言两者一致。
# 含义：无论清单怎么写，这些路径一律拒绝写入。
FLOOR_GLOBS: Tuple[str, ...] = (
    "data", "data/**",
    "venv/**", ".venv/**", "models/**", "backgrounds/**", "node_modules/**",
    "*.pem", "*.key", "*.crt", "*.lock", "*.env",
    ".git/**",
    "conviction.json", "long_horizon.json", "gene_stats.json",
    "reward_memory.json", "rlhf_model.json", "rl_bandit.json",
    "rl_interval.json", "rl_mode_stats.json", "rl_pushpull.json",
    "world_model.json", "music_playlists.json", "video_favorites.json",
    "video_history.json", "video_sources.json", "workspace_saved.json",
    "role_card_users.json", "agent_profiles.json", "harness_tasks.json",
    "harness_task_memory.json", "harness_state.json", "harness_bridge.json",
    "codex_runtime.json",
    "settings.json", "codex_config.json", "stt_config.json", "tts_config.json",
    "cards.json", "character_cards.json", "nodes.json",
    "chat_memory.db", "chat_memory.db-shm", "chat_memory.db-wal",
    "skills/*/data", "skills/*/data/**",
)

# 受管资产白名单：住在大资产目录里、但属于发布方受管、随包分发、可被更新覆盖。
# 与 paths.py 的 PACKED_ASSETS 同源，测试断言两者一致。
PACKED_ASSETS: Tuple[str, ...] = (
    "models/白头凤.vrm",
    "models/渡鸦将军.vrm",
)

# 经历见证集：更新前后比对这些文件的哈希，用来证明「经历没被动过」。
# 刻意不含 venv/models/backgrounds —— 那些是大资产，哈希它们只会拖慢更新，
# 而它们本来就不可再生性低（删了能重建）。
WITNESS_SKIP_DIRS = {
    "venv", ".venv", "models", "backgrounds", "node_modules", "audio_cache",
    "logs", "codex_logs", ".git", "dist", "web/anim", "web/vendor",
    "data/longrun", "data/sandboxes", "data/uploads", "data/backup", "data/locks",
    "data/android", "deploy/tls",
}
WITNESS_MAX_BYTES = 4 << 20


# ── 最小 glob 匹配（自带，不依赖仓库）─────────────────────────────────────
def _to_regex(pattern: str) -> "re.Pattern[str]":
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


_FLOOR_RX: List["re.Pattern[str]"] = []
for _g in FLOOR_GLOBS:
    _FLOOR_RX.append(_to_regex(_g))
    if _g.endswith("/**"):
        _FLOOR_RX.append(_to_regex(_g[:-3]))

_ASSET_RX: List["re.Pattern[str]"] = [_to_regex(_g) for _g in PACKED_ASSETS]


def norm(path: str) -> str:
    p = str(path).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def is_forbidden(path: str) -> bool:
    """地板判定。祖先目录命中即命中 —— 保护不依赖「当前有哪些文件」。"""
    p = norm(path)
    if not p:
        return True
    if any(r.match(p) for r in _ASSET_RX):
        return False
    if any(r.match(p) for r in _FLOOR_RX):
        return True
    parts = p.split("/")
    for k in range(1, len(parts)):
        if any(r.match("/".join(parts[:k])) for r in _FLOOR_RX):
            return True
    return False


def safe_join(base: Path, rel: str) -> Path:
    """把清单里的相对路径安全地接到 base 下，越界直接拒绝。"""
    r = norm(rel)
    if not r or r.startswith("/") or ".." in r.split("/"):
        raise ValueError(f"非法相对路径：{rel!r}")
    target = (base / r).resolve()
    root = base.resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"路径越出目标目录：{rel!r}")
    return target


# ── 配置与凭证 ───────────────────────────────────────────────────────────
CONF_FILE = Path("/etc/dabai/update.conf")
USER_CONF = Path.home() / ".config" / "dabai" / "update.conf"
SECRET_FILES = (Path("/etc/dabai/secrets.env"), Path.home() / ".config" / "dabai" / "secrets.env")


def load_conf() -> Dict[str, str]:
    cfg = dict(DEFAULTS)
    for p in (CONF_FILE, USER_CONF):
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def get_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    for p in SECRET_FILES:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def state_dir(cfg: Dict[str, str]) -> Path:
    """状态与备份放在仓库之外 —— 更新器自己的痕迹不该落进被更新的目录。"""
    for cand in (Path(cfg["STATE"]), Path.home() / ".local" / "state" / "dabai-update"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".writable"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return cand
        except OSError:
            continue
    raise SystemExit("✘ 找不到可写的状态目录")


def log_line(cfg: Dict[str, str], msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(state_dir(cfg) / "update.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── 版本 ─────────────────────────────────────────────────────────────────
def vkey(v: str) -> Tuple[int, ...]:
    bits = re.findall(r"\d+", str(v) or "")
    return tuple(int(b) for b in bits[:4]) if bits else (0,)


def local_version(root: Path, fallback: str = "0.0.0") -> str:
    f = root / "VERSION"
    if f.is_file():
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    return fallback


def updater_copy_stale(root: Path, me: Optional[Path] = None) -> Optional[Tuple[Path, Path]]:
    """本机正在跑的更新器副本，是否落后于仓库里那份。落后则返回 (副本, 仓库版)。

    更新器故意跑在仓库之外（仓库正是被更新的对象），代价是它更新不了自己：
    装发行版只刷新 <root>/deploy/release/update.py，而 systemd 跑的是
    /usr/local/lib/dabai-update/ 里那份副本，只有 install-update.sh 会换它。
    不查这一下，新能力就静默不生效 —— v1.0.0 正是如此：包里没有 --tag，
    装了它的机器反而切不了版本。
    """
    here = Path(me if me is not None else __file__).resolve()
    packaged = (Path(root) / "deploy" / "release" / "update.py").resolve()
    if here == packaged or not here.is_file() or not packaged.is_file():
        return None
    if hashlib.sha256(here.read_bytes()).hexdigest() == hashlib.sha256(packaged.read_bytes()).hexdigest():
        return None
    return (here, packaged)


def stale_updater_note(stale: Optional[Tuple[Path, Path]]) -> List[str]:
    if not stale:
        return []
    return [
        f"⚠ 更新器副本落后：本机跑的是 {stale[0]}，仓库里已是新版",
        "   它跑在仓库之外，更新不了自己 —— 新能力（如 --tag 版本切换）要重跑一次才生效：",
        "   sudo bash deploy/release/install-update.sh",
    ]


# ── GitHub ───────────────────────────────────────────────────────────────
def gh_request(url: str, token: str, timeout: int, raw: bool = False):
    headers = {
        "Accept": "application/octet-stream" if raw else "application/vnd.github+json",
        "User-Agent": "dabai-update",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if raw else json.loads(r.read().decode("utf-8"))


def latest_release(repo: str, token: str, timeout: int) -> Dict[str, Any]:
    return gh_request(f"https://api.github.com/repos/{repo}/releases/latest", token, timeout)


def release_by_tag(repo: str, tag: str, token: str, timeout: int) -> Dict[str, Any]:
    """按 tag 取发行版 —— 版本切换走这个端点，不是拿 --force 硬拉最新版。"""
    return gh_request(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token, timeout)


def pick_assets(release: Dict[str, Any], version: str) -> Tuple[Optional[str], Optional[str]]:
    """从 release 资产里挑出 tarball 与 sha256 的下载地址。"""
    tar_url = sha_url = None
    for a in release.get("assets", []) or []:
        name = a.get("name", "")
        if name.endswith(".tar.gz"):
            tar_url = a.get("url")
        elif name.endswith(".sha256"):
            sha_url = a.get("url")
    return tar_url, sha_url


# release-assets 会解析出多个 IP，其中个别地址连 443 无响应。urllib 单次尝试撞上
# 就白等到 HTTP_TIMEOUT 见底（60s），所以单次尝试用短超时，失败再试 —— 每次
# urlopen 都会重新解析地址表，重试不是重复同一次失败。
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_ATTEMPT_TIMEOUT = 15


def download(url: str, dest: Path, token: str, timeout: int,
             on_retry: Optional[Any] = None, attempts: int = DOWNLOAD_ATTEMPTS) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            data = gh_request(url, token, min(timeout, DOWNLOAD_ATTEMPT_TIMEOUT), raw=True)
        except Exception as ex:
            last = ex
            if i < attempts:
                if on_retry:
                    on_retry(i, ex)
                time.sleep(2 * i)
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return
    raise RuntimeError(f"下载失败（已尝试 {attempts} 次）：{last}")


def parse_sha256_file(text: str) -> str:
    for tok in text.replace("\n", " ").split():
        if len(tok) == 64 and all(c in "0123456789abcdefABCDEF" for c in tok):
            return tok.lower()
    return ""


# ── 最小清单校验（自带，不依赖仓库里的 manifest.py）────────────────────────
# 与 manifest.py 的 validate_manifest / verify_tree 有意重复。理由同文件头：
# 更新器不能依赖被更新的仓库。测试 test_validators_agree 断言两者判定一致。
REQUIRED_MANIFEST_FIELDS = (
    ("schema", int), ("version", str), ("built_at", str),
    ("built_on", str), ("entry", str), ("files", list),
)
REQUIRED_FILE_FIELDS = (
    ("path", str), ("sha256", str), ("size", int), ("mode", int),
)


def validate_manifest(m: Dict[str, Any]) -> List[str]:
    """结构校验。返回问题清单，空列表 = 通过。"""
    problems: List[str] = []
    for name, typ in REQUIRED_MANIFEST_FIELDS:
        if name not in m:
            problems.append(f"缺字段 {name}")
        elif not isinstance(m[name], typ):
            problems.append(f"字段 {name} 类型应为 {typ.__name__}")
    if problems:
        return problems
    if m["schema"] != SCHEMA_VERSION:
        problems.append(f"清单格式版本 {m['schema']} 与更新器要求的 {SCHEMA_VERSION} 不符")
    if not m["files"]:
        problems.append("files 为空 —— 空包不该发布")
    seen = set()
    for i, e in enumerate(m["files"]):
        if not isinstance(e, dict):
            problems.append(f"files[{i}] 不是对象")
            continue
        for name, typ in REQUIRED_FILE_FIELDS:
            if name not in e:
                problems.append(f"files[{i}] 缺 {name}")
            elif not isinstance(e[name], typ):
                problems.append(f"files[{i}].{name} 类型应为 {typ.__name__}")
        p = e.get("path", "")
        if isinstance(p, str):
            if p.startswith("/") or ".." in p.split("/"):
                problems.append(f"files[{i}].path 非法（绝对路径或含 ..）：{p}")
            if p in seen:
                problems.append(f"重复条目：{p}")
            seen.add(p)
        sha = e.get("sha256", "")
        if isinstance(sha, str) and len(sha) != 64:
            problems.append(f"files[{i}].sha256 长度不是 64：{p}")
    return problems


def verify_tree(m: Dict[str, Any], base: Path) -> Tuple[bool, List[str]]:
    """解包后逐个核对大小与 sha256。返回 (是否全对, 问题清单)。"""
    problems: List[str] = []
    for e in m.get("files", []):
        if not isinstance(e, dict):
            continue
        rel = norm(e.get("path", ""))
        try:
            p = safe_join(base, rel)
        except ValueError as ex:
            problems.append(str(ex))
            continue
        if not p.is_file():
            problems.append(f"缺文件：{rel}")
            continue
        if p.stat().st_size != e.get("size"):
            problems.append(f"大小不符：{rel}（清单 {e.get('size')}，实得 {p.stat().st_size}）")
            continue
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        want = str(e.get("sha256", "")).lower()
        if got != want:
            problems.append(f"内容不符：{rel}（清单 {want[:12]}…，实得 {got[:12]}…）")
    return (not problems), problems


# ── 解包 ─────────────────────────────────────────────────────────────────
def extract_package(tar_path: Path, dest: Path) -> Dict[str, Any]:
    """解到暂存区并读回清单。解包途中就拒绝受保护路径 —— 别等写到一半才发现。"""
    dest.mkdir(parents=True, exist_ok=True)
    man: Optional[Dict[str, Any]] = None
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            rel = norm(member.name)
            if not rel:
                continue
            if member.name.startswith("/") or ".." in rel.split("/"):
                raise SystemExit(f"✘ 包内含非法路径：{member.name}")
            if member.isdir():
                continue
            if rel == MANIFEST_NAME:
                # 清单直接读进内存，不落暂存区：它是「这次该写什么」的描述，
                # 本身不是待安装文件。落进去反而会被写入计划当成普通文件看待。
                blob = tar.extractfile(member)
                if blob is None:
                    raise SystemExit("✘ 读不出包内清单")
                try:
                    man = json.loads(blob.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as ex:
                    raise SystemExit(f"✘ 包内清单不是合法 JSON：{ex}")
                continue
            if is_forbidden(rel):
                raise SystemExit(
                    f"✘ 整包作废：包内出现受保护路径 {rel}\n"
                    f"   这不是「跳过这一条」的事 —— 发布方越界说明打包逻辑坏了，"
                    f"坏逻辑不会只坏一条。"
                )
            if not member.isfile():
                raise SystemExit(f"✘ 包内含非普通文件：{member.name}（软链/设备节点一律拒绝）")
            target = safe_join(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise SystemExit(f"✘ 读不出包内文件：{member.name}")
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if man is None:
        raise SystemExit("✘ 包里没有 MANIFEST.json")
    return man


# ── 经历见证集 ───────────────────────────────────────────────────────────
def _witness_skip(rel: str) -> bool:
    for d in WITNESS_SKIP_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return True
    return False


def witness(root: Path) -> Dict[str, str]:
    """给「经历」拍快照：仓库里所有受保护文件的内容哈希。

    只走受保护文件，且剪掉大资产目录 —— 哈希 625MB 的 venv 只会拖慢更新，
    而 venv 本来就不可再生性低（删了能重建），它不属于「经历」。
    更新前后各拍一次，两次之差就是「经历有没有被动过」的直接证据。
    """
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        cur = Path(dirpath)
        rel_dir = cur.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [
            d for d in dirnames
            if not _witness_skip(f"{rel_dir}/{d}".strip("/"))
        ]
        for fn in filenames:
            rel = f"{rel_dir}/{fn}".strip("/")
            if _witness_skip(rel) or not is_forbidden(rel):
                continue
            p = cur / fn
            try:
                st = p.stat()
                out[rel] = (f"size:{st.st_size}" if st.st_size > WITNESS_MAX_BYTES
                            else hashlib.sha256(p.read_bytes()).hexdigest())
            except OSError:
                continue
    return out


def witness_diff(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    diffs: List[str] = []
    for k in sorted(set(before) | set(after)):
        a, b = before.get(k), after.get(k)
        if a != b:
            diffs.append(f"{k}: {str(a)[:12]} → {str(b)[:12]}")
    return diffs


# ── 写入计划与执行 ───────────────────────────────────────────────────────
def plan_writes(man: Dict[str, Any], root: Path, staged: Path):
    """把清单变成写入计划。任何一条不合格 → 整包拒绝，不做部分更新。"""
    writes: List[Tuple[str, Path, Path]] = []
    problems: List[str] = []
    seen = set()
    for e in man.get("files", []):
        if not isinstance(e, dict):
            problems.append("清单里有非对象条目")
            continue
        rel = norm(e.get("path", ""))
        if not rel or rel in seen:
            problems.append(f"清单条目非法或重复：{e.get('path')!r}")
            continue
        seen.add(rel)
        if is_forbidden(rel):
            problems.append(f"清单要求写入受保护路径：{rel}")
            continue
        sha = str(e.get("sha256", "")).lower()
        if len(sha) != 64:
            problems.append(f"{rel}：sha256 不合法")
            continue
        try:
            src = safe_join(staged, rel)
            dst = safe_join(root, rel)
        except ValueError as ex:
            problems.append(str(ex))
            continue
        if not src.is_file():
            problems.append(f"{rel}：暂存区缺这个文件")
            continue
        got = hashlib.sha256(src.read_bytes()).hexdigest()
        if got != sha:
            problems.append(f"{rel}：内容哈希不符（清单 {sha[:12]}… 实得 {got[:12]}…）")
            continue
        writes.append((rel, src, dst))
    return writes, problems


def stale_files(man: Dict[str, Any], root: Path, prev_manifest: Optional[Dict[str, Any]]):
    """上一版有、这一版没有的代码文件。默认不动它们，只报告。"""
    if not prev_manifest:
        return []
    new_paths = {norm(e.get("path", "")) for e in man.get("files", []) if isinstance(e, dict)}
    out = []
    for e in prev_manifest.get("files", []):
        rel = norm(e.get("path", ""))
        if rel and rel not in new_paths and not is_forbidden(rel):
            out.append(rel)
    return sorted(out)


def apply_writes(writes, backup_dir: Path) -> List[List[str]]:
    """逐文件原子替换，旧内容留备份。返回 [[rel, 旧状态], ...] 供回滚。"""
    done: List[List[str]] = []
    backup_dir.mkdir(parents=True, exist_ok=True)
    for rel, src, dst in writes:
        if dst.exists():
            b = safe_join(backup_dir, rel)
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, b)
            prev = str(b)
        else:
            prev = "absent"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".new-upt")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, src.stat().st_mode & 0o777)
        os.replace(tmp, dst)          # 原子替换：断电不会留下半个文件
        done.append([rel, prev])
    return done


def write_journal(cfg: Dict[str, str], journal: Dict[str, Any]) -> Path:
    p = state_dir(cfg) / "rollback_journal.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(journal, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def read_journal(cfg: Dict[str, str]) -> Optional[Dict[str, Any]]:
    p = state_dir(cfg) / "rollback_journal.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def restore(journal: Dict[str, Any]) -> List[str]:
    """按日志回滚。返回问题清单，空 = 干净还原。"""
    problems: List[str] = []
    root = Path(journal.get("root", ""))
    for rel, prev in reversed(journal.get("entries", [])):
        try:
            dst = safe_join(root, rel)
        except ValueError as ex:
            problems.append(str(ex))
            continue
        try:
            if prev == "absent":
                if dst.exists():
                    dst.unlink()
            elif Path(prev).is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prev, dst)
            else:
                problems.append(f"{rel}：备份文件已不在（{prev}）")
        except OSError as ex:
            problems.append(f"{rel}：还原失败 {ex}")
    return problems


# ── 服务控制与健康检查 ───────────────────────────────────────────────────
def svc(action: str, service: str, timeout: int = 120) -> Tuple[int, str]:
    """控制 systemd 服务。非 root 时走 sudo -n，要求已配窄口径免密规则。"""
    cmd = ["systemctl", action, service]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "找不到 systemctl 或 sudo"
    except subprocess.TimeoutExpired:
        return 124, f"{action} 超时"
    return p.returncode, (p.stdout + p.stderr).strip()


def svc_active(service: str) -> str:
    _rc, out = svc("is-active", service)
    out = out.strip()
    return out.splitlines()[0] if out else "unknown"


def health(cfg: Dict[str, str], service: str, port: str, settle: float = 8.0) -> Tuple[bool, str]:
    """起服务后的体检：systemd 状态 + 端口 + HTTP 探活。

    只看 systemd 状态不够 —— 进程活着但端口没起来（依赖没装、配置错）也算坏。
    任何 <500 的 HTTP 回应都算「服务在答话」：这个端点可能要求鉴权，401/403 也是活的。
    """
    time.sleep(settle)
    state = svc_active(service)
    if state != "active":
        return False, f"systemd 状态 {state!r}（期望 active）"
    deadline = time.time() + 40
    last = "还没探到"
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
                pass
        except OSError as ex:
            last = f"端口 {port} 未通（{ex}）"
            time.sleep(2)
            continue
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/frontend-build")
            with urllib.request.urlopen(req, timeout=8) as r:
                return True, f"端口 {port} 通，HTTP {r.status}"
        except urllib.error.HTTPError as ex:
            if ex.code < 500:
                return True, f"端口 {port} 通，HTTP {ex.code}"
            last = f"HTTP {ex.code}"
        except Exception as ex:
            last = f"HTTP 探活异常（{ex}）"
        time.sleep(2)
    return False, f"端口 {port} 探活失败：{last}"


def active_turn(root: Path, window: float = 120.0) -> Optional[str]:
    """有正在进行的对话轮就别重启。更新可以等五分钟，用户的话等不了。"""
    d = root / "data" / "turn_checkpoints"
    if not d.is_dir():
        return None
    newest = 0.0
    try:
        for p in d.iterdir():
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return None
    if newest and (time.time() - newest) < window:
        return time.strftime("%H:%M:%S", time.localtime(newest))
    return None


# ── 主流程 ───────────────────────────────────────────────────────────────
def do_rollback(cfg: Dict[str, str], args) -> int:
    j = read_journal(cfg)
    if not j:
        log_line(cfg, "✘ 没有可用的回滚日志")
        return 1
    n = len(j.get("entries", []))
    log_line(cfg, f"回滚：v{j.get('to_version')} → v{j.get('from_version')}（{n} 个文件）")
    if args.dry_run:
        log_line(cfg, "（演练模式，未动盘）")
        return 0
    if not args.no_restart:
        rc, out = svc("stop", cfg["SERVICE"])
        log_line(cfg, f"  停机 rc={rc} {out}")
    problems = restore(j)
    if not args.no_restart:
        rc, out = svc("start", cfg["SERVICE"])
        log_line(cfg, f"  起服务 rc={rc} {out}")
    if problems:
        for p in problems[:10]:
            log_line(cfg, f"  ! {p}")
        return 1
    log_line(cfg, "✔ 回滚完成")
    return 0


def run(args) -> int:
    cfg = load_conf()
    for key, val in (("ROOT", args.root), ("STATE", args.state), ("REPO", args.repo),
                     ("SERVICE", args.service), ("PORT", args.port)):
        if val:
            cfg[key] = val
    if args.keep_backups:
        cfg["KEEP_BACKUPS"] = str(args.keep_backups)

    root = Path(cfg["ROOT"]).resolve()
    if not (root / cfg["ENTRY"]).is_file():
        log_line(cfg, f"✘ {root} 里找不到 {cfg['ENTRY']}，不像安装目录")
        return 2

    if args.rollback:
        return do_rollback(cfg, args)

    cur = local_version(root)
    stage = state_dir(cfg) / "staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    # ── ① 取包 ──────────────────────────────────────────────────────────
    if args.local_tarball:
        tar_path = Path(args.local_tarball).resolve()
        if not tar_path.is_file():
            log_line(cfg, f"✘ 找不到本地包 {tar_path}")
            return 2
        sha_file = tar_path.with_name(tar_path.name + ".sha256")
        want = parse_sha256_file(sha_file.read_text(encoding="utf-8")) if sha_file.is_file() else ""
        if not want:
            want = hashlib.sha256(tar_path.read_bytes()).hexdigest()
            log_line(cfg, "  ! 没有 .sha256 文件，改用本地计算值（仅离线测试可用）")
        src_note = f"本地包 {tar_path.name}"
    else:
        token = get_token()
        if not token:
            log_line(cfg, "✘ 没找到 GITHUB_TOKEN（环境变量 → /etc/dabai/secrets.env → ~/.config/dabai/secrets.env）")
            return 2
        try:
            if args.tag:
                rel = release_by_tag(cfg["REPO"], args.tag, token, int(cfg["HTTP_TIMEOUT"]))
            else:
                rel = latest_release(cfg["REPO"], token, int(cfg["HTTP_TIMEOUT"]))
        except urllib.error.HTTPError as ex:
            what = f"发行版 {args.tag}" if args.tag else "最新发行版"
            log_line(cfg, f"✘ 查{what}失败：HTTP {ex.code}")
            if ex.code == 404 and args.tag:
                log_line(cfg, "   该 tag 下没有发行版（tag 存在但没建 release 也是这个错）")
            return 1
        except Exception as ex:
            log_line(cfg, f"✘ 查最新发行版失败：{ex}")
            return 1
        remote_ver = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
        if not remote_ver:
            log_line(cfg, "✘ 最新发行版没有版本号")
            return 1
        if args.check:
            for line in stale_updater_note(updater_copy_stale(root)):
                log_line(cfg, line)
            if vkey(remote_ver) > vkey(cur) or args.force:
                log_line(cfg, f"有新版：本地 v{cur} → 远端 v{remote_ver}")
                return 10
            log_line(cfg, f"已是最新：v{cur}（远端 v{remote_ver}）")
            return 0
        tar_url, sha_url = pick_assets(rel, remote_ver)
        if not tar_url:
            log_line(cfg, f"✘ 发行版 v{remote_ver} 没有 .tar.gz 资产")
            return 1
        if not sha_url:
            log_line(cfg, "✘ 发行版没带 .sha256 资产 —— 无法校验，拒绝更新")
            return 1
        tar_path = stage / f"dabai-{remote_ver}.tar.gz"
        log_line(cfg, f"下载 v{remote_ver} …")
        try:
            download(tar_url, tar_path, token, int(cfg["HTTP_TIMEOUT"]),
                     on_retry=lambda n, ex: log_line(cfg, f"   第 {n} 次下载没成（{ex}），重试 …"))
            want = parse_sha256_file(
                gh_request(sha_url, token, int(cfg["HTTP_TIMEOUT"]), raw=True).decode("utf-8", "replace"))
        except Exception as ex:
            log_line(cfg, f"✘ 下载失败：{ex}")
            return 1
        if not want:
            log_line(cfg, "✘ .sha256 资产里读不出合法哈希")
            return 1
        src_note = f"GitHub Release v{remote_ver}"

    # ── ② 校验包哈希 ────────────────────────────────────────────────────
    got = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    if got != want:
        log_line(cfg, f"✘ 包哈希不符，拒绝更新\n   期望 {want[:16]}…\n   实得 {got[:16]}…")
        return 1
    log_line(cfg, f"① 包哈希校验通过（{got[:16]}…）  来源：{src_note}")

    # ── ③ 解包并校验清单 ────────────────────────────────────────────────
    try:
        man = extract_package(tar_path, stage)
    except SystemExit as ex:
        log_line(cfg, str(ex))
        return 1
    ver = str(man.get("version") or "")
    problems = validate_manifest(man)
    if problems:
        log_line(cfg, "✘ 清单不合法：")
        for p in problems[:10]:
            log_line(cfg, f"   {p}")
        return 1
    forbidden = [norm(e["path"]) for e in man["files"] if is_forbidden(norm(e["path"]))]
    if forbidden:
        log_line(cfg, "✘ 整包作废：清单要求写入受保护路径")
        for p in forbidden[:20]:
            log_line(cfg, f"   {p}")
        return 1
    ok, bad = verify_tree(man, stage)
    if not ok:
        log_line(cfg, "✘ 包内文件与清单不符：")
        for b in bad[:10]:
            log_line(cfg, f"   {b}")
        return 1
    log_line(cfg, f"② 清单与逐文件 sha256 全对（{man['file_count']} 个文件）")

    # ── ④ 版本判定 ──────────────────────────────────────────────────────
    # 点名 --tag 就是要这个版本，降级也算数 —— 版本切换本来就是往旧版走
    if not args.force and not args.tag and vkey(ver) <= vkey(cur):
        log_line(cfg, f"跳过：远端 v{ver} 不比本地 v{cur} 新（要强制就加 --force）")
        return 0
    if args.check:
        log_line(cfg, f"有新版：本地 v{cur} → 远端 v{ver}")
        return 10
    log_line(cfg, f"③ 版本 {cur} → {ver}")

    # ── ⑤ 写入计划 ──────────────────────────────────────────────────────
    writes, wproblems = plan_writes(man, root, stage)
    if wproblems:
        log_line(cfg, "✘ 写入计划有问题，整包拒绝：")
        for p in wproblems[:10]:
            log_line(cfg, f"   {p}")
        return 1
    prev_man = read_journal(cfg)
    stale = stale_files(man, root, (prev_man or {}).get("manifest"))
    log_line(cfg, f"④ 写入计划：{len(writes)} 个文件"
                  + (f"，另有 {len(stale)} 个旧文件不在新包里（默认保留，要删加 --prune）" if stale else ""))

    if args.dry_run:
        for rel, _s, dst in writes[:15]:
            log_line(cfg, f"   [演练] 会写 {rel} → {dst}")
        if len(writes) > 15:
            log_line(cfg, f"   [演练] …另 {len(writes) - 15} 个")
        log_line(cfg, "（演练模式：未停机、未写盘、未重启）")
        return 0

    if not args.no_restart and not args.ignore_active_turn:
        at = active_turn(root)
        if at:
            log_line(cfg, f"跳过本次：{at} 还有对话轮在跑。更新可以等，用户的话等不了。")
            return 20

    # ── ⑥ 停机 + 经历快照 ───────────────────────────────────────────────
    stopped = False
    if not args.no_restart:
        rc, out = svc("stop", cfg["SERVICE"])
        log_line(cfg, f"⑤ 停机 rc={rc} {out}")
        stopped = True
    before = witness(root)
    log_line(cfg, f"⑥ 经历快照：{len(before)} 个受保护文件已记下哈希")

    # ── ⑦ 写入 ──────────────────────────────────────────────────────────
    backup_dir = state_dir(cfg) / "backups" / f"v{cur}"
    try:
        entries = apply_writes(writes, backup_dir)
        if args.prune and stale:
            pruned = 0
            for rel in stale:
                try:
                    dst = safe_join(root, rel)
                    b = safe_join(backup_dir, rel)
                except ValueError:
                    continue
                if not dst.is_file():
                    continue
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, b)
                dst.unlink()
                entries.append([rel, str(b)])
                pruned += 1
            log_line(cfg, f"⑦′ 清理新包里已不存在的旧代码 {pruned} 个（已备份，可回滚）")
    except Exception as ex:
        log_line(cfg, f"✘ 写入过程出错：{ex}")
        if stopped:
            svc("start", cfg["SERVICE"])
        return 1
    write_journal(cfg, {
        "from_version": cur,
        "to_version": ver,
        "root": str(root),
        "backup_dir": str(backup_dir),
        "entries": entries,
        "manifest": man,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    log_line(cfg, f"⑦ 已替换 {len(entries)} 个文件（旧版备份在 {backup_dir}）")

    # ── ⑧ 经历复核 ──────────────────────────────────────────────────────
    after = witness(root)
    diffs = witness_diff(before, after)
    if diffs:
        log_line(cfg, f"✘ 经历文件被改动了 {len(diffs)} 处 —— 这是 bug，立即回滚：")
        for d in diffs[:10]:
            log_line(cfg, f"   {d}")
        restore(read_journal(cfg) or {})
        if stopped:
            svc("start", cfg["SERVICE"])
        return 1
    log_line(cfg, f"⑧ 经历复核通过：{len(before)} 个文件哈希逐个未变")

    # ── ⑨ 起服务 + 体检 ─────────────────────────────────────────────────
    if not args.no_restart:
        rc, out = svc("start", cfg["SERVICE"])
        log_line(cfg, f"⑨ 起服务 rc={rc} {out}")
        ok_h, detail = health(cfg, cfg["SERVICE"], cfg["PORT"])
        if not ok_h:
            log_line(cfg, f"✘ 体检未通过：{detail} —— 自动回滚")
            svc("stop", cfg["SERVICE"])
            problems = restore(read_journal(cfg) or {})
            for p in problems[:5]:
                log_line(cfg, f"   ! 回滚问题：{p}")
            svc("start", cfg["SERVICE"])
            ok2, detail2 = health(cfg, cfg["SERVICE"], cfg["PORT"], settle=6.0)
            log_line(cfg, f"{'✔ 回滚后服务恢复' if ok2 else '✘ 回滚后仍不正常，需要人工介入'}：{detail2}")
            return 1
        log_line(cfg, f"⑨ 体检通过：{detail}")

    (state_dir(cfg) / "current_version").write_text(ver + "\n", encoding="utf-8")
    log_line(cfg, f"✔ 更新完成：v{cur} → v{ver}")

    # ── ⑩ 更新器副本自检 ────────────────────────────────────────────────
    # 更新成功不等于能力到齐：副本是旧的，这次装上的新功能照样用不了。
    for line in stale_updater_note(updater_copy_stale(root)):
        log_line(cfg, line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="大白节点侧自动更新器")
    ap.add_argument("--root", default="", help="安装目录")
    ap.add_argument("--state", default="", help="状态/备份目录")
    ap.add_argument("--repo", default="", help="GitHub 仓库 owner/name")
    ap.add_argument("--service", default="", help="systemd 服务名")
    ap.add_argument("--port", default="", help="服务端口")
    ap.add_argument("--check", action="store_true", help="只看有没有新版（默认行为）")
    ap.add_argument("--dry-run", action="store_true", help="全流程演练：不写盘、不重启")
    ap.add_argument("--apply", action="store_true", help="真更新")
    ap.add_argument("--rollback", action="store_true", help="回滚到上一版")
    ap.add_argument("--tag", default="", help="切到指定版本（如 v1.0.0），默认取最新发行版")
    ap.add_argument("--local-tarball", default="", help="离线/测试：直接用本地包")
    ap.add_argument("--local-manifest", default="", help="离线/测试：配套清单（可选）")
    ap.add_argument("--force", action="store_true", help="同版本或降级也执行")
    ap.add_argument("--prune", action="store_true", help="删除新包里已不存在的旧代码文件")
    ap.add_argument("--no-restart", action="store_true", help="不碰服务（测试用）")
    ap.add_argument("--ignore-active-turn", action="store_true", help="有对话轮在跑也照更")
    ap.add_argument("--keep-backups", type=int, default=0)
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
