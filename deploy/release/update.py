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
import grp
import gzip
import hashlib
import json
import os
import pwd
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
# 交给独立 unit 时打的标记：调用方（peer_autoupdate）靠它区分「已移交」和「已装好」
DETACH_MARK = "[detached]"

# ── 配置默认值（可被 /etc/dabai/update.conf 或命令行覆盖）──────────────────
DEFAULTS = {
    "ROOT": "/home/wxf/dabai",
    "STATE": "/var/lib/dabai-update",
    "REPO": "geek-alpha/dabai-linux",
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


def _probe_writable(d: Path) -> bool:
    """这个目录（连同它自己）真能建、真能写吗 —— 不是看 mode，是写入探一次。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def state_candidates(cfg: Dict[str, str]) -> Tuple[Path, ...]:
    return (Path(cfg["STATE"]), Path.home() / ".local" / "state" / "dabai-update")


def state_dir(cfg: Dict[str, str]) -> Path:
    """状态与备份放在仓库之外 —— 更新器自己的痕迹不该落进被更新的目录。

    探测下探到子目录级：光「父目录能写」不算数。实测踩过的坑（2026-09-20，orangepi）：
    /var/lib/dabai-update 本身可写，探测照样通过，可里面的 staging 子目录属 root:root；
    候选被选中后，run() 里那句 rmtree(ignore_errors=True) 又把「清不掉」吞掉，
    三行之后才以「下载失败（已尝试 3 次）：[Errno 13] Permission denied: …staging/xxx.part」
    报出来 —— 看着像网络故障，实际是权限，还白等两轮重试。
    所以真正要往里写的 staging / backups 两个子目录一并探；探不过就换下一个候选，
    都探不过才报错，并且报错里直接给能原地执行的修法。
    """
    for cand in state_candidates(cfg):
        if not _probe_writable(cand):
            continue
        if all(_probe_writable(cand / sub) for sub in ("staging", "backups")):
            return cand
    tried = "、".join(str(c) for c in state_candidates(cfg))
    raise SystemExit(
        "✘ 找不到可写的状态目录（staging/backups 至少得能写）\n"
        f"   试过：{tried}\n"
        f"   若是历史遗留的 root 属主目录，原地修：sudo chown -R $(id -u):$(id -g) {cfg['STATE']}"
    )


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


# ── 代理探测（内嵌副本，理由同文件头：更新器不依赖仓库内文件）────────────
# 直连 GitHub 常见 60KB/s 量级（26MB 要 6 分钟，还常中途超时断掉）；本机跑着
# sing-box / clash 时同一个包 7 秒下完。代理开着、链路不知道它存在 —— 这就是慢
# 的全部原因。不猜只探：环境变量 → 本机常用端口 → 都没有就直连（没代理的机器
# 行为完全不变）。deploy/release/netproxy.py 是同一份逻辑，改这里时那边也要改。
PROXY_PORTS = (7890, 7891, 10809, 1080, 10808, 20171)
_proxy_cache: Optional[str] = None
_proxy_probed = False


def detect_proxy() -> Optional[str]:
    global _proxy_cache, _proxy_probed
    if _proxy_probed:
        return _proxy_cache
    _proxy_probed = True
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            _proxy_cache = val
            return _proxy_cache
    for port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                _proxy_cache = f"http://127.0.0.1:{port}"
                return _proxy_cache
        except OSError:
            continue
    return None


# ── 国内镜像路由（内嵌副本，理由同文件头）────────────────────────────────
# 有代理走代理直连；没代理时 GitHub 直连是国内最慢的一段（60KB/s 量级，26MB
# 要 6 分钟还常超时），自动切 ghproxy 类国内加速前缀。环境变量 DABAI_GITHUB_MIRROR
# 可显式指定（如 https://ghproxy.net/，部署时写进 /etc/dabai/proxy.env 或 export）。
# 候选逐个轻量探测，第一个能用的锁住；全挂回退直连 —— 没镜像的机器行为不变。
MIRROR_PREFIXES = (
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
)
MIRROR_PROBE_TIMEOUT = 8
_mirror_cache: Optional[str] = None
_mirror_probed = False


def mirror_prefix() -> Optional[str]:
    """当前生效的镜像前缀；有代理或探测不到可用镜像时返回 None（直连）。"""
    global _mirror_cache, _mirror_probed
    if _mirror_probed:
        return _mirror_cache
    _mirror_probed = True
    if detect_proxy():
        return None                      # 有代理就直连，镜像只是没代理时的兜底
    override = os.environ.get("DABAI_GITHUB_MIRROR", "").strip().rstrip("/")
    if override:
        _mirror_cache = override + "/"
        return _mirror_cache
    for prefix in MIRROR_PREFIXES:
        try:
            probe = urllib.request.Request(
                prefix + "https://api.github.com/rate_limit",
                headers={"User-Agent": "dabai-update"})
            with urllib.request.urlopen(probe, timeout=MIRROR_PROBE_TIMEOUT) as r:
                if r.status < 500:
                    _mirror_cache = prefix
                    return _mirror_cache
        except Exception:
            continue
    return None


def route_url(url: str) -> str:
    """有代理直连；无代理时给 GitHub 官方域名套镜像前缀。API 与资产下载同走。"""
    mp = mirror_prefix()
    if not mp:
        return url
    if url.startswith("https://api.github.com/") or url.startswith("https://github.com/"):
        return mp + url
    return url


def _opener():
    """带代理的 opener；没代理时显式直连 —— 不被环境里残留的坏变量带跑。"""
    proxy = detect_proxy()
    mapping = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(mapping))


# ── GitHub ───────────────────────────────────────────────────────────────
def gh_request(url: str, token: str, timeout: int, raw: bool = False):
    url = route_url(url)
    headers = {
        "Accept": "application/octet-stream" if raw else "application/vnd.github+json",
        "User-Agent": "dabai-update",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with _opener().open(req, timeout=timeout) as r:
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
# 就白等到 HTTP_TIMEOUT 见底（60s），所以建连用短超时，失败再试 —— 每次 urlopen
# 都会重新解析地址表，重试不是重复同一次失败。
#
# 短超时治得了建连，治不了慢速下载：树莓派直连 GitHub 约 60KB/s，26MB 要 6 分钟
# 以上，整包一次读完必然撞超时（09-20 那次连试三次全挂就是它）。所以整包改成分块
# Range 请求：
#   · socket 超时是「空闲超时」不是总时长 —— 数据在流动就不会触发，慢不等于失败；
#   · 首块故意取小（建连慢/坏 IP 15 秒内暴露），后续块给足 120 秒空闲；
#   · 块断了只重下这一块，已落盘的部分不重来；
#   · 流式追加写盘，不再把整个包读进内存。
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_ATTEMPT_TIMEOUT = 15          # 建连 + 首块：坏 IP 快速跳过
DOWNLOAD_IDLE_TIMEOUT = 120            # 数据：多久没有新字节才算死链
DOWNLOAD_FIRST_CHUNK = 64 * 1024       # 首块 64KB
DOWNLOAD_CHUNK = 8 * 1024 * 1024       # 后续每块 8MB（块越小握手次数越多，实测 2MB 拖慢 4 倍）


def _range_get(url: str, token: str, start: int, size: int, timeout: int):
    """取 [start, start+size) 一段。返回 (数据, 状态码, 文件总大小或 None)。"""
    url = route_url(url)
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "dabai-update",
        "X-GitHub-Api-Version": "2022-11-28",
        "Range": f"bytes={start}-{start + size - 1}",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with _opener().open(req, timeout=timeout) as r:
        total = None
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                total = None
        elif r.status == 200:
            cl = (r.headers.get("Content-Length") or "").strip()
            total = int(cl) if cl.isdigit() else None
        return r.read(), r.status, total


def _fetch_whole(url: str, token: str, tmp: Path) -> None:
    """分块把整个文件落到 tmp。"""
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pos = 0
    total = None
    while total is None or pos < total:
        first = pos == 0
        size = DOWNLOAD_FIRST_CHUNK if first else DOWNLOAD_CHUNK
        data, status, seen_total = _range_get(
            url, token, pos, size,
            DOWNLOAD_ATTEMPT_TIMEOUT if first else DOWNLOAD_IDLE_TIMEOUT)
        if status == 200:
            # 服务器忽略 Range（镜像/代理会这样）：一次返回的就是整包，写完即止 ——
            # 再请求只会拿到同一个整包，循环里拼就是无限重复
            tmp.write_bytes(data)
            return
        if seen_total is not None:
            total = seen_total
        if not data:
            break
        with tmp.open("wb" if pos == 0 else "ab") as f:
            f.write(data)
        pos += len(data)
    if total is not None and pos != total:
        raise RuntimeError(f"下载不完整：{pos}/{total} 字节")


def download(url: str, dest: Path, token: str,
             on_retry: Optional[Any] = None, attempts: int = DOWNLOAD_ATTEMPTS) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Optional[Exception] = None
    for i in range(1, attempts + 1):
        if tmp.exists():
            tmp.unlink()      # 上一轮残片不拼进这一轮：脏字节别产生，别指望 sha256 兜底
        try:
            _fetch_whole(url, token, tmp)
        except PermissionError as ex:
            # 权限错重试三次只会把「写不进暂存目录」伪装成网络故障，还白等 2+4 秒。
            raise RuntimeError(
                f"写不进暂存目录：{ex}\n   目录属主不是当前用户？"
                f"原地修：sudo chown -R $(id -u):$(id -g) {dest.parent}"
            ) from ex
        except Exception as ex:
            last = ex
            if i < attempts:
                if on_retry:
                    on_retry(i, ex)
                time.sleep(2 * i)
            continue
        tmp.replace(dest)
        return
    tmp.unlink(missing_ok=True)
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


def _in_service_cgroup(service: str, cgroup_text: Optional[str] = None) -> bool:
    """自己是不是跑在目标服务的 cgroup 里。

    是的话 systemctl stop 的 KillMode=control-group 会把更新器一起杀掉 ——
    文件没换、服务停着起不来，日志里只剩一行「⑤ 停机」。
    """
    if not service:
        return False
    names = {service, service if service.endswith(".service") else service + ".service"}
    if cgroup_text is None:
        try:
            cgroup_text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        except OSError:
            return False
    for line in cgroup_text.splitlines():
        leaf = line.rsplit(":", 1)[-1].strip().rsplit("/", 1)[-1]
        if leaf in names:
            return True
    return False


def _token_in_file() -> bool:
    """token 能不能只靠文件拿到。能就别从 argv 接 —— argv 同机任何用户可读。"""
    for p in SECRET_FILES:
        try:
            if p.is_file() and "GITHUB_TOKEN=" in p.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def _run_user_for(cfg: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """脱离出来的 unit 该以谁的身份跑 —— 答案是「安装目录的属主」，不是 root。

    为什么必须钉死：systemd-run 默认落系统级 unit，User= 缺省是 root。而整套更新器
    按设计是「普通用户跑，只在重启服务这一件事上升权」（见 install-update.sh 文件头）。
    升权一旦覆盖到写盘，代价立刻可见：root 写出的 staging/backups 属主是 root，
    下一次普通用户跑的更新器就再也写不进同一份暂存目录 —— 报出来的却是「下载失败」。
    实测 2026-09-20 orangepi：✘ Permission denied: /var/lib/dabai-update/staging/…part。
    解析不出属主就返回 None（维持旧行为），调用方负责把这件事记进日志。
    """
    try:
        st = Path(cfg.get("ROOT") or ".").stat()
        return (pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name)
    except (OSError, KeyError):
        return None


def detach_self(cfg: Dict[str, str], argv: Sequence[str]) -> int:
    """把自己交给 systemd-run 起的独立 unit，本进程立即退出。

    新 unit 与目标服务平级、cgroup 互不相干，stop 目标服务不会再杀掉更新器。
    日志照旧双写 state_dir/update.log，脱离终端不影响取证。
    """
    unit = f"dabai-updater-{int(time.time())}"
    cmd = ["systemd-run", "--unit", unit, "--collect", "--quiet",
           "--property=Type=oneshot",
           f"--property=WorkingDirectory={os.getcwd()}"]
    # 升权只用来「起一个与目标服务平级的 unit」，不该让整套更新改以 root 跑
    owner = _run_user_for(cfg)
    if owner:
        cmd.append(f"--property=User={owner[0]}")
        cmd.append(f"--property=Group={owner[1]}")
    else:
        log_line(cfg, "  ! 解析不出安装目录属主，脱离出来的 unit 按调用者身份跑"
                      "（调用者是 root 时，暂存/备份会变成 root 属主）")
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok and not _token_in_file():
        # 文件里没有才靠环境变量带过去。代价：systemd-run 的 argv 会短暂暴露这个值。
        # 接受它是因为另一种结局更差 —— 新 unit 找不到 token，更新直接失败。
        cmd.append(f"--setenv=GITHUB_TOKEN={tok}")
    cmd += ["--", sys.executable, str(Path(__file__).resolve()), *argv, "--detached"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        log_line(cfg, "! 没有 systemd-run，无法脱离服务 cgroup —— 本次不更新"
                      "（原地跑会在 ⑤ 停机那步把自己杀掉，服务起不来）")
        return 1
    except subprocess.TimeoutExpired:
        log_line(cfg, "! systemd-run 超时 —— 本次不更新")
        return 1
    if p.returncode != 0:
        detail = (p.stdout + p.stderr).strip() or "无输出"
        log_line(cfg, f"! 脱离 cgroup 失败（rc={p.returncode}）：{detail} —— 本次不更新")
        return 1
    log_line(cfg, f"{DETACH_MARK} 已交给独立 unit {unit} 继续更新（本进程退出，结果见 update.log）")
    return 0


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


# ── 工作区闸门 ───────────────────────────────────────────────────────────
def _git(root: Path, *args: str) -> Optional[str]:
    """跑一条 git 命令，成功返回 stdout；不是仓库 / 没装 git 返回 None。"""
    if not shutil.which("git"):
        return None
    try:
        p = subprocess.run(("git", "-C", str(root)) + args,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _last_apply_path(cfg: Dict[str, str]) -> Path:
    return state_dir(cfg) / "last_apply.json"


def read_last_apply(cfg: Dict[str, str]) -> Dict[str, str]:
    """上次更新写进安装目录的 {相对路径: sha256}；没记录过就是空表。"""
    try:
        d = json.loads(_last_apply_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = d.get("files")
    return files if isinstance(files, dict) else {}


def write_last_apply(cfg: Dict[str, str], ver: str, root: Path, entries) -> None:
    """记下这次写了哪些文件、写成什么内容。

    下次判定「工作区脏」时要靠它区分「人改的」和「更新自己改的」—— 更新器按设计
    不碰 .git，所以每更新一次，被替换的文件相对 HEAD 都会显示为已修改。
    """
    files: Dict[str, str] = {}
    for rel, _prev in entries:
        try:
            files[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            continue
    try:
        _last_apply_path(cfg).write_text(
            json.dumps({"version": ver, "files": files,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as ex:
        log_line(cfg, f"  ! 记不下本次写入清单（{ex}）—— 下次可能把更新造成的改动误判成人工改动")


def worktree_changes(root: Path, cfg: Dict[str, str]) -> List[str]:
    """安装目录是 git 工作区、且有人改了没提交时，返回那些文件；否则空表。

    为什么必须拦：更新器只认包清单，不看工作区里有没有没提交的东西。安装目录恰好
    就是开发现场时（本机就是），自动更新会把「改了还没提交」的文件静默盖回发布版 ——
    版本号往前走了，工作内容却退回去了，日志里还只写着「已替换 N 个文件」。

    为什么要排除上次更新的痕迹：那些文件的内容等于 last_apply 记下的哈希，git 说它脏
    只是因为 .git 没跟着走。不排除就是死锁 —— 更新过一次之后永远判定为脏，从此再也
    不更新，功能看着装了其实失效。
    """
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return []            # 不是 git 工作区（同伴的安装目录常常不是），不拦
    out = _git(root, "status", "--porcelain", "--untracked-files=no")
    if not out:
        return []
    files = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        if " -> " in rel:                       # 重命名写成 "旧 -> 新"
            rel = rel.split(" -> ", 1)[1]
        if rel:
            files.append(rel)
    if not files:
        return []
    done = read_last_apply(cfg)
    if not done:
        return files
    left = []
    for rel in files:
        want = done.get(rel)
        if want:
            try:
                if hashlib.sha256((root / rel).read_bytes()).hexdigest() == want:
                    continue                # 这就是上次更新写的，不算人工改动
            except OSError:
                pass
        left.append(rel)
    return left


def _skipped_path(cfg: Dict[str, str]) -> Path:
    return state_dir(cfg) / "skipped.json"


def _session_buses(uid: int) -> List[Path]:
    """能拿来发桌面通知的总线：目标用户的、当前会话的，谁在算谁。"""
    cands: List[Path] = []
    env = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if env.startswith("unix:path="):
        cands.append(Path(env.split("=", 1)[1]))
    for u in (uid, os.geteuid()):
        cands.append(Path(f"/run/user/{u}/bus"))
    out, seen = [], set()
    for c in cands:
        if c in seen or not c.exists():
            continue
        seen.add(c)
        out.append(c)
    return out


def _desktop_notify(cfg: Dict[str, str], title: str, body: str) -> bool:
    """更新器以 root 跑，通知得落到登录会话的总线上；落不下去就只留日志，不算失败。"""
    exe = shutil.which("notify-send") or "/usr/bin/notify-send"
    if not Path(exe).is_file():
        return False
    try:
        st = Path(cfg["ROOT"]).stat()
    except OSError:
        return False
    for bus in _session_buses(st.st_uid):
        try:
            owner = bus.stat().st_uid
            gid = pwd.getpwuid(owner).pw_gid
        except (OSError, KeyError):
            continue
        env = dict(os.environ)
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        env["XDG_RUNTIME_DIR"] = str(bus.parent)
        cmd = [exe, title, body]
        if os.geteuid() == 0 and owner != 0 and shutil.which("setpriv"):
            cmd = ["setpriv", f"--reuid={owner}", f"--regid={gid}",
                   "--init-groups", *cmd]
        try:
            if subprocess.run(cmd, env=env, capture_output=True,
                              timeout=15).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def record_skip(cfg: Dict[str, str], dirty: List[str], local_ver: str,
                remote_ver: str, reason: str) -> bool:
    """记下「有新版但没装上」并提醒一次 —— 静默落后，等于更新器没装。"""
    behind = bool(remote_ver) and vkey(remote_ver) > vkey(local_ver)
    try:
        prev = json.loads(_skipped_path(cfg).read_text(encoding="utf-8"))
        prev = prev if isinstance(prev, dict) else {}
    except (OSError, ValueError):
        prev = {}
    rec = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "local": local_ver, "remote": remote_ver, "behind": behind,
        "reason": reason, "dirty_count": len(dirty), "dirty": dirty[:20],
        "notified_remote": str(prev.get("notified_remote") or ""),
    }
    if behind and rec["notified_remote"] != remote_ver:
        told = _desktop_notify(
            cfg, f"大白：有新版没装上 v{remote_ver}",
            f"本机停在 v{local_ver}，安装目录有 {len(dirty)} 个未提交改动，本次跳过。"
            f"提交并发布后会自动跟上。")
        if told:
            rec["notified_remote"] = remote_ver
    try:
        _skipped_path(cfg).write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return behind


def worktree_gate(cfg: Dict[str, str], root: Path, args,
                  remote_ver: str = "") -> int:
    """有未提交的工作就拒绝自动覆盖，返回 21；放行返回 0。"""
    if args.force:
        return 0
    dirty = worktree_changes(root, cfg)
    if not dirty:
        return 0
    cur = local_version(root)
    if remote_ver and vkey(remote_ver) > vkey(cur):
        # 发布源这台机器版本天然领先，走到这儿说明别处发了比本机新的版本
        log_line(cfg, f"⚠ 远端 v{remote_ver} 比本机 v{cur} 新（别处发布的），但安装目录有"
                      f" {len(dirty)} 个未提交改动 —— 本次跳过，本机仍停在 v{cur}")
    else:
        log_line(cfg, f"跳过本次：安装目录里有 {len(dirty)} 个改了没提交的文件"
                      "，自动更新会把它们盖回发布版")
    for rel in dirty[:10]:
        log_line(cfg, f"   {rel}")
    if len(dirty) > 10:
        log_line(cfg, f"   …另 {len(dirty) - 10} 个")
    log_line(cfg, "   先提交并发布（deploy/release/publish.py），或加 --force 接受覆盖")
    record_skip(cfg, dirty, cur, remote_ver, "dirty-worktree")
    return 21


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
    # 回滚是救命路径，更不能死在停机上：更新失败 + 服务停死 + 回滚没做完是最坏结局
    if not args.no_restart and not args.detached and _in_service_cgroup(cfg["SERVICE"]):
        return detach_self(cfg, sys.argv[1:])
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


def do_status(cfg: Dict[str, str], args) -> int:
    """一条命令看清：本机在哪一版、上次更新是什么时候、有没有「有新版但没装上」。"""
    root = Path(cfg["ROOT"]).resolve()
    cur = local_version(root)
    print(f"安装目录  {root}")
    print(f"本机版本  v{cur}")
    try:
        cv = (state_dir(cfg) / "current_version").read_text(encoding="utf-8").strip()
        print(f"上次装到  v{cv}")
    except OSError:
        print("上次装到  （没有记录 —— 从没自动更新过）")
    try:
        d = json.loads(_last_apply_path(cfg).read_text(encoding="utf-8"))
        print(f"上次写入  v{d.get('version')} @ {d.get('at')}，"
              f"{len(d.get('files') or {})} 个文件")
    except (OSError, ValueError):
        print("上次写入  （无）")
    j = read_journal(cfg)
    if j:
        print(f"可回滚到  v{j.get('from_version')}（备份 {j.get('backup_dir')}）")
    try:
        sk = json.loads(_skipped_path(cfg).read_text(encoding="utf-8"))
        sk = sk if isinstance(sk, dict) else None
    except (OSError, ValueError):
        sk = None
    if sk and sk.get("behind"):
        print(f"⚠ 有新版没装上：远端 v{sk.get('remote')} 比本机 v{sk.get('local')} 新")
        print(f"   原因：{sk.get('dirty_count')} 个未提交改动（{sk.get('at')}）")
        for rel in (sk.get("dirty") or [])[:10]:
            print(f"     {rel}")
        print("   提交并发布后会自动跟上；想直接覆盖加 --force")
    elif sk:
        print(f"上次跳过  {sk.get('at')}：{sk.get('reason')}"
              f"（远端 v{sk.get('remote') or '—'}）")
    else:
        print("跳过记录  （无）")
    dirty = worktree_changes(root, cfg)
    print("工作区    干净" if not dirty else f"工作区    {len(dirty)} 个未提交改动")
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

    if getattr(args, "status", False):
        return do_status(cfg, args)

    if args.rollback:
        return do_rollback(cfg, args)

    cur = local_version(root)
    stage = state_dir(cfg) / "staging"
    shutil.rmtree(stage, ignore_errors=True)
    if stage.exists():
        # state_dir 已经保证这个目录可写，所以剩下的解释只有一种：别的进程正在用同一份暂存。
        log_line(cfg, f"  ! 旧暂存目录没清干净，继续用 {stage}")
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
        _proxy = detect_proxy()
        log_line(cfg, f"  网络：{'走代理 ' + _proxy if _proxy else '直连（未探测到本机代理）'}")
        if not token:
            # 公开仓匿名可拉：没凭据不再是失败，只是拿不到私有仓而已。
            # 私有仓匿名访问返回 404（不是 401），下面按这个事实给提示。
            log_line(cfg, "  没找到 GITHUB_TOKEN，按公开仓匿名访问")

        try:
            if args.tag:
                rel = release_by_tag(cfg["REPO"], args.tag, token, int(cfg["HTTP_TIMEOUT"]))
            else:
                rel = latest_release(cfg["REPO"], token, int(cfg["HTTP_TIMEOUT"]))
        except urllib.error.HTTPError as ex:
            what = f"发行版 {args.tag}" if args.tag else "最新发行版"
            log_line(cfg, f"✘ 查{what}失败：HTTP {ex.code}")
            if ex.code == 404 and not token:
                log_line(cfg, "   匿名访问得到 404：仓库是私有的，或确实没有发行版")
                log_line(cfg, "   私有仓需要凭据：环境变量 → /etc/dabai/secrets.env → ~/.config/dabai/secrets.env")
            elif ex.code == 404 and args.tag:
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
        # 版本判定放在下载之前：远端 tag 已经拿到了，没必要为了「发现自己不用更新」
        # 先下 28MB 包、解 1030 个文件再说 —— 树莓派上这笔开销是分钟级，而且每次开机都付。
        if not args.force and not args.tag and vkey(remote_ver) <= vkey(cur):
            log_line(cfg, f"跳过：远端 v{remote_ver} 不比本地 v{cur} 新（要强制就加 --force）")
            for line in stale_updater_note(updater_copy_stale(root)):
                log_line(cfg, line)
            return 0

        # 安装目录里有没提交的工作时，绝不自动覆盖 —— 版本可以落后，工作不能丢。
        # 放在下载之前：脏工作区是「不更新」的充分理由，没必要为它先下 28MB 包。
        gate = worktree_gate(cfg, root, args, remote_ver)
        if gate:
            return gate

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
            download(tar_url, tar_path, token,
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

    # --local-tarball 不经过上面的远端分支，闸门在这里补一次（远端路径已经拦过，
    # 能走到这儿说明它是干净的，重复检查只是一次 git status）。
    gate = worktree_gate(cfg, root, args, ver)
    if gate:
        return gate

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

    # 自己就跑在目标服务里时，⑤ 停机会连自己一起杀 —— 先挪进独立 cgroup 再动手
    if not args.no_restart and not args.detached and _in_service_cgroup(cfg["SERVICE"]):
        return detach_self(cfg, sys.argv[1:])

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
    write_last_apply(cfg, ver, root, entries)

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
    try:
        _skipped_path(cfg).unlink()      # 跟上了，落后记录作废
    except OSError:
        pass
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
    ap.add_argument("--status", action="store_true",
                    help="看本机版本/上次更新/有没有「有新版没装上」")
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
    ap.add_argument("--detached", action="store_true",
                    help="内部用：已脱离服务 cgroup，不再二次脱离")
    ap.add_argument("--keep-backups", type=int, default=0)
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
