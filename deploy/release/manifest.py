#!/usr/bin/env python3
"""发行包清单（MANIFEST）—— 发布方与更新方之间的唯一契约。

为什么需要一份清单，而不是「把 tar 解开覆盖过去」：
  更新方在动手写盘之前，必须先能回答两个问题 —— 这一版该写哪些文件？
  其中有没有一条是我绝不能写的？清单让第二个问题在**下载后、写盘前**就能答完。
  清单里出现受保护路径，不是「跳过那一条」，而是整包作废：发布方越界说明打包逻辑
  坏了，坏逻辑不会只坏一条。

字段冻结在 SCHEMA_VERSION。任何一侧改字段，另一侧必须同步改并升版本号。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

SCHEMA_VERSION = 1
MANIFEST_NAME = "MANIFEST.json"

# 清单必需字段：(名字, 类型, 说明)
REQUIRED_FIELDS: Tuple[Tuple[str, type, str], ...] = (
    ("schema", int, "清单格式版本"),
    ("version", str, "发行版本号（语义化，如 1.2.0）"),
    ("built_at", str, "打包时间 ISO8601"),
    ("built_on", str, "打包所在实例名"),
    ("entry", str, "启动入口，相对路径"),
    ("files", list, "文件条目数组，见 FILE_FIELDS"),
)

FILE_FIELDS: Tuple[Tuple[str, type, str], ...] = (
    ("path", str, "相对仓库根的正斜杠路径"),
    ("sha256", str, "文件内容哈希（64 位十六进制）"),
    ("size", int, "字节数"),
    ("mode", int, "权限位（十进制，如 420=0644、493=0755）"),
)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_rel(path: str) -> str:
    """归一化成相对路径。

    不能用 `lstrip("./")` —— lstrip 剥的是字符集，会把 `.gitattributes` 吃成
    `gitattributes`，隐藏文件全部丢名字。这个坑在 2026-09-18 被解包回验当场抓到。
    """
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def build_manifest(
    files: Iterable[Tuple[str, Path]],
    *,
    version: str,
    entry: str = "server.py",
    commit: str = "",
    built_on: str = "",
    built_at: str = "",
    excluded: Dict[str, int] | None = None,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """files 是 (相对路径, 绝对路径) 序列。相对路径统一用正斜杠。"""
    entries: List[Dict[str, Any]] = []
    for rel, abs_path in files:
        rel = norm_rel(rel)
        st = abs_path.stat()
        entries.append({
            "path": rel,
            "sha256": sha256_file(abs_path),
            "size": st.st_size,
            "mode": st.st_mode & 0o777,
        })
    entries.sort(key=lambda e: e["path"])
    m: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "version": version,
        "built_at": built_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "built_on": built_on,
        "entry": entry,
        "commit": commit,
        "file_count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "files": entries,
    }
    if excluded:
        m["excluded"] = dict(excluded)
    if extra:
        m.update(extra)
    return m


def write_manifest(manifest: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def read_manifest(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("清单不是一个 JSON 对象")
    return data


def validate_manifest(m: Dict[str, Any]) -> List[str]:
    """结构校验。返回问题清单，空列表 = 通过。"""
    problems: List[str] = []
    for name, typ, _desc in REQUIRED_FIELDS:
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
        for name, typ, _desc in FILE_FIELDS:
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


def check_against_floor(
    m: Dict[str, Any], is_forbidden
) -> List[str]:
    """把清单里每条路径都过一遍保护地板。

    is_forbidden 是调用方注入的判定函数（更新器传自己内嵌的地板，打包器传 paths.classify）。
    注入而不是直接 import，是为了让更新器能带着自己那份冻结的判定跑 —— 清单和判定必须
    来自两个独立来源，否则「发布方出错」和「更新方放行」会同时发生。
    """
    bad: List[str] = []
    for e in m.get("files", []):
        p = e.get("path")
        if isinstance(p, str) and is_forbidden(p):
            bad.append(p)
    return bad


def verify_tree(m: Dict[str, Any], base: Path) -> Tuple[bool, List[str]]:
    """解包后逐个核对 sha256 与大小。返回 (是否全对, 问题清单)。"""
    problems: List[str] = []
    base = Path(base)
    for e in m.get("files", []):
        p = base / e["path"]
        if not p.is_file():
            problems.append(f"缺文件：{e['path']}")
            continue
        if p.stat().st_size != e["size"]:
            problems.append(f"大小不符：{e['path']}（清单 {e['size']}，实得 {p.stat().st_size}）")
            continue
        got = sha256_file(p)
        if got != e["sha256"]:
            problems.append(f"内容不符：{e['path']}（清单 {e['sha256'][:12]}…，实得 {got[:12]}…）")
    return (not problems), problems


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：manifest.py <MANIFEST.json>   # 校验结构并打印摘要")
        raise SystemExit(2)
    man = read_manifest(Path(sys.argv[1]))
    errs = validate_manifest(man)
    if errs:
        print("✘ 清单不合法：")
        for e in errs:
            print("   ", e)
        raise SystemExit(1)
    print(f"✔ 清单合法：v{man['version']}  {man['file_count']} 个文件  "
          f"{man['total_bytes'] / 1024:.0f} KB  打包于 {man['built_on']} {man['built_at']}")
