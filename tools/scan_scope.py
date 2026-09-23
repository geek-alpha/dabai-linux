#!/usr/bin/env python3
"""扫描范围 —— 「哪些目录不算项目自己的代码」的单一来源。

以前这份名单写了两遍：tools/deps_audit.py 的 SKIP_DIRS 和 skills/code_ops 的
NOISE_DIRS。同类名单分叉的代价是双向的 —— 一边排了另一边没排，报告里就混进
vendored 第三方符号（实测 tools/vendor_ots 的 gitdb/Cryptodome 霸占影响面 Top12）。

但两份名单不该完全相同，口径本来就不同：

    COMMON_DIRS  两边都排：虚拟环境、依赖缓存、构建产物、第三方 vendored 代码、
                 编辑器/工具缓存。这些目录里的东西都不是大白自己的代码。
    DEPS_EXTRA   只有依赖扫描排。data/ 下有轮快照（含被改文件的代码副本），
                 models/backgrounds 是二进制资源 —— 它们混进依赖报告是噪音，
                 但用户自己的脚本可能就放在这些目录里，代码搜索不该挡。
    CODE_EXTRA   只有代码搜索排：编辑器与前端构建缓存。

前缀匹配是必要的，不是锦上添花：精确匹配挡不住命名变体，实测 tools/vendor_ots
（12M / 260 个 py）因为多了后缀漏网。
"""
from __future__ import annotations

# 两边都排。
COMMON_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".ruff_cache", ".pytest_cache", ".mypy_cache", ".pytest_libs", "site-packages",
    ".tox", ".eggs", "vendor", "third_party",
    "codex_logs", "audio_cache", "undefined",
    # 整体拷进来的外部工程，不是大白的代码
    "godot-dabai", "mmd_tools_new",
})

# 命名变体：vendor / vendor_ots / third_party_x 都算噪音。
NOISE_PREFIXES = ("vendor", "third_party", ".pytest_libs")

# 只有依赖扫描排（代码搜索不排 —— 用户脚本可能在这些目录里）。
DEPS_EXTRA = frozenset({"data", "logs", "models", "backgrounds"})

# 只有代码搜索排（编辑器与前端构建缓存，里面没有可分析的源码）。
CODE_EXTRA = frozenset({
    ".idea", ".vscode", ".trae-html-share-packages", ".next", ".nuxt", "coverage",
})


def is_noise_dir(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """目录名是否该跳过。extra 传 DEPS_EXTRA 或 CODE_EXTRA 区分口径。"""
    return name in COMMON_DIRS or name in extra or name.startswith(NOISE_PREFIXES)


def is_dep_scan_noise(name: str) -> bool:
    """依赖扫描口径：COMMON + DEPS_EXTRA。"""
    return is_noise_dir(name, DEPS_EXTRA)


def is_code_scan_noise(name: str) -> bool:
    """代码搜索口径：COMMON + CODE_EXTRA。"""
    return is_noise_dir(name, CODE_EXTRA)


def split_path(parts) -> bool:
    """路径片段里任一段是噪音目录 → True。"""
    return any(is_dep_scan_noise(p) for p in parts)
