#!/usr/bin/env python3
"""发行版文件三分类 —— 全仓唯一权威。

仓库里同时住着两样东西：

  基因组  代码骨架 + 种子配置（*.example.json）。三台机器出生时一模一样。
  经历    信条、长期事业、基因统计、记忆、任务、联邦身份。每台各不相同，
          而且**不可再生** —— 被覆盖一次，那个实例就不再是它自己了。

所以「更新」这个动作的写入面，必须先被这份清单框死。判定顺序
LOCAL > EXPERIENCE > CODE，前两档一票否决：不是「尽量别写」，是「写了就是 bug」。

用法：
    from paths import classify, is_code, code_files
    classify("agent.py")            -> "code"
    classify("conviction.json")     -> "experience"
    classify("venv/bin/python")     -> "local"
    code_files(Path("/home/wxf/dabai"))   -> 该仓库里可被更新覆盖的文件清单

自检：python paths.py --selftest
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Sequence

CODE = "code"
EXPERIENCE = "experience"
LOCAL = "local"


# ── 经历：per-instance，跨机器无意义，覆盖即不可逆 ──────────────────────────
# 每一条都写明「为什么它属于经历而不是配置」—— 后来者想删某条时，先读这行。
EXPERIENCE_GLOBS: tuple[str, ...] = (
    # 我自己的判断与长期方向：被别人的版本覆盖 = 换了个脑子
    "conviction.json",
    "long_horizon.json",
    # 基因/强化学习：本地学习进度，跨机器无意义
    "gene_stats.json",
    "reward_memory.json",
    "rlhf_model.json",
    "rl_bandit.json",
    "rl_interval.json",
    "rl_mode_stats.json",
    "rl_pushpull.json",
    "world_model.json",
    # 用户的私人清单与偏好
    "music_playlists.json",
    "video_favorites.json",
    "video_history.json",
    "video_sources.json",
    "workspace_saved.json",
    "role_card_users.json",
    # 用户自建的智能体档案（内置 5 个在代码里，这几个是用户后加的）
    "agent_profiles.json",
    # 任务系统运行态：断点续跑状态，覆盖 = 丢掉正在跑的活
    "harness_tasks.json",
    "harness_task_memory.json",
    "harness_state.json",
    "harness_bridge.json",
    "codex_runtime.json",
    # data/ 整个目录：记忆库、联邦身份与收件箱、游标、断点、上传件
    "data",
    "data/**",
    # 技能自带的数据目录（如 skills/tasks/data/tasks.json 是用户的待办）
    "skills/*/data",
    "skills/*/data/**",
    "skills/*/tmp/**",
    # 运行时锁：进程正在持有，覆盖会撞车
    "*.lock",
)

# ── 本机私有：环境、凭证、大资产。换台机器就没用，或者根本不该传播 ──────────
LOCAL_GLOBS: tuple[str, ...] = (
    # 环境与大资产（体积大、可重建、跨机器无意义）
    "venv/**",
    ".venv/**",
    "node_modules/**",
    "dist/**",
    "models/**",
    "backgrounds/**",
    "audio_cache/**",
    "web/generated/**",
    "mmd_tools_new/**",
    # 日志与备份
    "logs/**",
    "codex_logs/**",
    "*.log",
    "*.bak",
    "*.bak-*",
    "*.testbak",
    "nul",
    "NUL",
    # 凭证：证书私钥、联邦共享密钥、SMTP、用户库
    "*.pem",
    "*.key",
    "*.crt",
    "deploy/tls/**",
    "deploy/secrets/*.json",
    "deploy/secrets/*.env",
    "data/cluster.key",
    "data/auth_secret",
    "data/smtp.json",
    "data/users.json",
    "data/users/**",
    # 本机配置：LLM 供应商/语音/角色卡/节点地址，每台不一样
    "settings.json",
    "codex_config.json",
    "stt_config.json",
    "tts_config.json",
    "cards.json",
    "character_cards.json",
    "nodes.json",
    "chat_memory.db",
    "chat_memory.db-shm",
    "chat_memory.db-wal",
    # 缓存与编译产物
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    ".ruff_cache/**",
    ".playwright-mcp/**",
    ".archive/**",
)

# ── 受管资产：住在大资产目录里，但属于发布方受管、随包分发 ──────────────────
# models/** 与 backgrounds/** 默认是 LOCAL（本机私有、换机器无意义），但这两个
# 角色模型是前端加载的运行时资源：cards.example.json 的种子卡就指向它们，新机器
# 缺了 3D 角色就是空的。所以逐个点名放行 —— 与 LOCAL 的「默认拒绝」相反，
# 以后往 models/ 里丢新文件仍然默认受保护。
PACKED_ASSETS: tuple[str, ...] = (
    "models/白头凤.vrm",
    "models/渡鸦将军.vrm",
)

# ── 保护地板：update.py 内嵌一份同样的最小集，与清单取并集 ──────────────────
# 为什么要两份：MANIFEST 是「发布方说该写什么」，地板是「更新器自己说绝不能写什么」。
# 发布方出 bug、包被投毒、清单被改坏时，地板是最后一道闸。
# 测试 test_floor_matches_paths 断言两者不漂移。
FLOOR_GLOBS: tuple[str, ...] = (
    "data",
    "data/**",
    "venv/**",
    ".venv/**",
    "models/**",
    "backgrounds/**",
    "node_modules/**",
    "*.pem",
    "*.key",
    "*.crt",
    "*.lock",
    "*.env",
    ".git/**",
    "conviction.json",
    "long_horizon.json",
    "gene_stats.json",
    "reward_memory.json",
    "rlhf_model.json",
    "rl_bandit.json",
    "rl_interval.json",
    "rl_mode_stats.json",
    "rl_pushpull.json",
    "world_model.json",
    "music_playlists.json",
    "video_favorites.json",
    "video_history.json",
    "video_sources.json",
    "workspace_saved.json",
    "role_card_users.json",
    "agent_profiles.json",
    "harness_tasks.json",
    "harness_task_memory.json",
    "harness_state.json",
    "harness_bridge.json",
    "codex_runtime.json",
    "settings.json",
    "codex_config.json",
    "stt_config.json",
    "tts_config.json",
    "cards.json",
    "character_cards.json",
    "nodes.json",
    "chat_memory.db",
    "chat_memory.db-shm",
    "chat_memory.db-wal",
    "skills/*/data",
    "skills/*/data/**",
)


def _to_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 编译成正则。支持 ** / * / ?，与 gitignore 语义对齐：

    区分 `*`（不跨目录）与 `**`（跨目录）很关键 —— 若把 `*` 也当跨目录，
    `skills/*/data/**` 会误伤 `skills/a/b/data/x`，保护范围悄悄变大。
    """
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")   # `**/` 可匹配零层目录
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


_CACHE: dict[str, List[re.Pattern[str]]] = {}


def _regexes(globs: Sequence[str]) -> List[re.Pattern[str]]:
    key = "\x00".join(globs)
    hit = _CACHE.get(key)
    if hit is None:
        pats: List[re.Pattern[str]] = []
        for g in globs:
            pats.append(_to_regex(g))
            # `data/**` 要连目录本身（`data`）一起保护，否则删除整个目录的写法能绕过
            if g.endswith("/**"):
                pats.append(_to_regex(g[:-3]))
        _CACHE[key] = hit = pats
    return hit


def _norm(path: str | Path) -> str:
    p = str(path).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def _matches(path: str, globs: Sequence[str]) -> bool:
    if not path:
        return False
    if any(r.match(path) for r in _regexes(globs)):
        return True
    # 祖先目录命中即命中：`data/**` 必须拦住 `data/a/b/c.json` 的任意深度，
    # 也要拦住未来新增的子路径 —— 保护不能依赖「当前有哪些文件」。
    parts = path.split("/")
    for k in range(1, len(parts)):
        if any(r.match("/".join(parts[:k])) for r in _regexes(globs)):
            return True
    return False


def classify(path: str | Path) -> str:
    """返回 code / experience / local。前两档一律不许更新写入。"""
    p = _norm(path)
    if _matches(p, PACKED_ASSETS):
        return CODE
    if _matches(p, LOCAL_GLOBS):
        return LOCAL
    if _matches(p, EXPERIENCE_GLOBS):
        return EXPERIENCE
    return CODE


def is_code(path: str | Path) -> bool:
    return classify(path) == CODE


def is_protected(path: str | Path) -> bool:
    """经历或本机私有 —— 更新写入面的禁区。"""
    return classify(path) != CODE


def floor_violation(path: str | Path) -> bool:
    """只用地板判一次：与 classify 互相独立，用于交叉校验。"""
    p = _norm(path)
    if _matches(p, PACKED_ASSETS):
        return False
    return _matches(p, FLOOR_GLOBS)


def tracked_files(root: str | Path) -> List[str]:
    """git 跟踪的文件清单（相对路径，正斜杠）。"""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    return [f for f in out.split("\0") if f]


def code_files(root: str | Path, files: Iterable[str] | None = None) -> List[str]:
    """该仓库里「可被更新覆盖」的文件清单 = 被跟踪 ∧ 非经历 ∧ 非本机私有。"""
    src = list(files) if files is not None else tracked_files(root)
    return sorted(f for f in src if is_code(f))


def _selftest() -> int:
    cases = [
        # (路径, 期望分类)
        ("agent.py", CODE),
        ("harness/core.py", CODE),
        ("deploy/release/update.py", CODE),
        ("skills/peer/SKILL.md", CODE),
        ("character_cards.example.json", CODE),  # 种子是基因组，属于代码
        # 经历
        ("conviction.json", EXPERIENCE),
        ("long_horizon.json", EXPERIENCE),
        ("gene_stats.json", EXPERIENCE),
        ("data/gene_exposure.jsonl", EXPERIENCE),
        ("data/sub_agents.jsonl", EXPERIENCE),
        ("data/peer_inbox.jsonl", EXPERIENCE),
        ("data/a/b/c/deep.json", EXPERIENCE),  # 祖先命中，未来新增路径也拦得住
        ("skills/tasks/data/tasks.json", EXPERIENCE),
        ("codex_runtime.json", EXPERIENCE),
        ("harness_bridge.json", EXPERIENCE),
        # 本机私有
        ("venv/bin/python", LOCAL),
        ("models/x.vrm", LOCAL),
        ("models/白头凤.vrm", CODE),           # 受管资产：点名放行
        ("models/渡鸦将军.vrm", CODE),
        ("models/白头凤_draco3.vrm", LOCAL),   # 未点名 → 默认受保护
        ("backgrounds/太空飞船走廊.glb", LOCAL),
        ("key.pem", LOCAL),
        ("deploy/tls/dabai-server.key", LOCAL),
        ("settings.json", LOCAL),
        ("cards.json", LOCAL),
        ("chat_memory.db-wal", LOCAL),
        ("logs/server.log", LOCAL),
        ("x/y/__pycache__/z.pyc", LOCAL),
    ]
    bad = 0
    for path, want in cases:
        got = classify(path)
        mark = "✔" if got == want else "✘"
        if got != want:
            bad += 1
            print(f"  {mark} {path}: 期望 {want}，实得 {got}")
    # 地板必须覆盖每一条字面量经历路径（带通配的条目由上面的样例覆盖）。
    # 这条断言是留给未来的：往 EXPERIENCE_GLOBS 加东西却忘了加地板，自检当场翻脸。
    for g in EXPERIENCE_GLOBS:
        if "*" in g:
            continue
        if not floor_violation(g):
            bad += 1
            print(f"  ✘ 地板漏了经历路径：{g}")
    print(f"paths 自检：{len(cases)} 例，失败 {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    files = code_files(root)
    print(f"仓库 {root}：可更新文件 {len(files)} 个")
    for f in files[:20]:
        print("  ", f)
    if len(files) > 20:
        print(f"   …另 {len(files) - 20} 个")
