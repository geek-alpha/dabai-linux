#!/usr/bin/env python3
"""打包发行版 —— 从仓库生成一个能被三台机器自动安装的包。

产物（默认落在 deploy/release/dist/）：
    dabai-<version>.tar.gz          只含代码。经历与本机私有文件在打包阶段就被排除，
                                    不是在安装阶段被跳过 —— 少一层「指望对方守规矩」。
    dabai-<version>.tar.gz.sha256   包哈希，更新方的第一道校验
    MANIFEST.json                   包内清单的副本，不下载就能先看要写哪些文件

可复现构建：文件按路径排序、mtime/uid/gid 归零、gzip mtime=0。
同一个 commit 打两次包，字节完全一致 —— 这样「包哈希变了」才真的意味着内容变了。

用法：
    python build_release.py                    # 用 VERSION 里的版本号打包
    python build_release.py --bump patch       # 版本号 +1 后打包
    python build_release.py --list             # 只列出会进包的文件
    python build_release.py --no-verify        # 跳过解包回验（不推荐）
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
ROOT_DEFAULT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import manifest as M  # noqa: E402
import paths as P  # noqa: E402

ENTRY = "server.py"
VERSION_FILE = "VERSION"


def git_head_meta(root: Path) -> Tuple[str, int]:
    """返回 (commit sha, 提交时间 epoch)。

    打包时间取**提交时间**而不是「现在」：否则同一个 commit 打两次包会得到两个
    不同的哈希，「包哈希变了」就不再等于「内容变了」。
    无 git 时退到 SOURCE_DATE_EPOCH，最后才用当前时间。
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H%n%ct"], cwd=str(root),
            capture_output=True, check=True, timeout=15,
        ).stdout.decode().split()
        return out[0], int(out[1])
    except Exception:
        return "", int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))


def read_version(root: Path) -> str:
    f = root / VERSION_FILE
    if f.is_file():
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    return "1.0.0"


def bump(version: str, part: str) -> str:
    bits = (version.split(".") + ["0", "0", "0"])[:3]
    try:
        major, minor, patch = (int(b) for b in bits)
    except ValueError:
        raise SystemExit(f"版本号无法解析：{version}（要形如 1.2.3）")
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def collect(root: Path) -> Tuple[List[Tuple[str, Path]], List[str], Dict[str, List[str]]]:
    """返回 (可打包文件, 磁盘缺失的跟踪文件, 被排除的清单)。"""
    tracked = P.tracked_files(root)
    excluded: Dict[str, List[str]] = {P.EXPERIENCE: [], P.LOCAL: []}
    include: List[Tuple[str, Path]] = []
    missing: List[str] = []
    for rel in tracked:
        cls = P.classify(rel)
        if cls != P.CODE:
            excluded[cls].append(rel)
            continue
        abs_path = root / rel
        if abs_path.is_file():
            include.append((rel, abs_path))
        else:
            missing.append(rel)
    return include, missing, excluded


def build_tar(pairs: List[Tuple[str, Path]], manifest: Dict, out: Path, epoch: int = 0) -> str:
    """可复现 tar.gz。返回包内容的 sha256。所有时间戳钉在 epoch（提交时间）上。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel, abs_path in sorted(pairs, key=lambda x: x[0]):
            ti = tar.gettarinfo(str(abs_path), arcname=rel)
            ti.mtime = epoch
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = abs_path.stat().st_mode & 0o777
            with open(abs_path, "rb") as fh:
                tar.addfile(ti, fh)
        blob = (json.dumps(manifest, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
        ti = tarfile.TarInfo(M.MANIFEST_NAME)
        ti.size = len(blob)
        ti.mtime = epoch
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(blob))
    payload = raw.getvalue()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=epoch, compresslevel=9) as gz:
            gz.write(payload)
    return M.sha256_bytes(payload)


def verify_package(tar_path: Path, manifest: Dict) -> List[str]:
    """解包回验：包内文件逐个核对 sha256，且绝不允许出现受保护路径。"""
    problems: List[str] = []
    with tempfile.TemporaryDirectory(prefix="dabai-rel-verify-") as td:
        tmp = Path(td)
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                name = M.norm_rel(member.name)
                if name.startswith("/") or ".." in name.split("/"):
                    problems.append(f"包内含非法路径：{member.name}")
                    continue
                tar.extract(member, tmp, filter="data")
        inner = tmp / M.MANIFEST_NAME
        if not inner.is_file():
            problems.append("包里没有 MANIFEST.json")
            return problems
        got = M.read_manifest(inner)
        if got.get("version") != manifest.get("version"):
            problems.append("包内清单版本号与预期不符")
        problems.extend(M.validate_manifest(got))
        ok, bad = M.verify_tree(got, tmp)
        if not ok:
            problems.extend(bad[:10])
        # 包内不得存在任何受保护路径 —— 这是「经历不会被覆盖」的第一道结构性保证
        for member in tarfile.open(tar_path, "r:gz").getmembers():
            rel = M.norm_rel(member.name)
            if rel == M.MANIFEST_NAME:
                continue
            if P.is_protected(rel):
                problems.append(f"包内出现受保护路径：{rel}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="打包大白发行版")
    ap.add_argument("--root", default=str(ROOT_DEFAULT), help="仓库根目录")
    ap.add_argument("--out", default="", help="产物目录（默认 <root>/deploy/release/dist）")
    ap.add_argument("--version", default="", help="显式指定版本号")
    ap.add_argument("--bump", choices=["major", "minor", "patch"], default="", help="打包前先升版本")
    ap.add_argument("--list", action="store_true", help="只列出会进包的文件")
    ap.add_argument("--no-verify", action="store_true", help="跳过解包回验")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not (root / ENTRY).is_file():
        print(f"✘ {root} 不像仓库根：找不到 {ENTRY}")
        return 2

    version = args.version or read_version(root)
    if args.bump:
        version = bump(version, args.bump)
        (root / VERSION_FILE).write_text(version + "\n", encoding="utf-8")

    pairs, missing, excluded = collect(root)
    if not pairs:
        print("✘ 没有任何可打包的代码文件")
        return 1

    if args.list:
        print(f"版本 {version}：{len(pairs)} 个文件会进包")
        for rel, _ in sorted(pairs):
            print("   ", rel)
        print(f"被排除：跟踪文件中的经历 {len(excluded[P.EXPERIENCE])} 个，本机私有 {len(excluded[P.LOCAL])} 个")
        for rel in excluded[P.EXPERIENCE]:
            print(f"    [经历] {rel}")
        return 0

    sha, epoch = git_head_meta(root)
    man = M.build_manifest(
        pairs,
        version=version,
        entry=ENTRY,
        commit=sha,
        built_on=socket.gethostname(),
        built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        excluded={k: len(v) for k, v in excluded.items()},
    )

    errs = M.validate_manifest(man)
    if errs:
        print("✘ 生成的清单不合法：")
        for e in errs:
            print("   ", e)
        return 1

    # 硬闸：清单里出现受保护路径 = 打包逻辑坏了，宁可不出包
    bad = M.check_against_floor(man, P.is_protected)
    if bad:
        print("✘ 清单里出现受保护路径，拒绝打包：")
        for b in bad[:20]:
            print("   ", b)
        return 1

    out_dir = Path(args.out) if args.out else HERE / "dist"
    tar_path = out_dir / f"dabai-{version}.tar.gz"
    digest = build_tar(pairs, man, tar_path, epoch=epoch)
    (out_dir / f"dabai-{version}.tar.gz.sha256").write_text(
        f"{digest}  dabai-{version}.tar.gz\n", encoding="utf-8")
    M.write_manifest(man, out_dir / f"dabai-{version}.MANIFEST.json")
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    size_mb = tar_path.stat().st_size / 1048576
    print(f"✔ 已打包 {tar_path.name}  {man['file_count']} 个文件  {size_mb:.2f} MB")
    print(f"  内容 sha256（gzip 前）：{digest[:16]}…")
    print(f"  包文件 sha256：{M.sha256_file(tar_path)[:16]}…")
    print(f"  排除（跟踪文件中的）：经历 {len(excluded[P.EXPERIENCE])} 个 / 本机私有 {len(excluded[P.LOCAL])} 个")
    print("  经历文件已不在跟踪面内，本就不在包的取材范围内 —— 这是结构性保证，不靠排除表")
    if missing:
        print(f"  ! 跟踪但磁盘缺失（未进包）：{len(missing)} 个，例：{missing[:3]}")

    if not args.no_verify:
        problems = verify_package(tar_path, man)
        if problems:
            print("✘ 解包回验未通过：")
            for p in problems[:20]:
                print("   ", p)
            return 1
        print("✔ 解包回验通过：sha256 全对、包内无受保护路径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
