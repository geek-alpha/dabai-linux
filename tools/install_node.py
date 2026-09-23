#!/usr/bin/env python3
"""Windows 上自动安装 Node.js —— 前端 .ts 实时转译的硬依赖。

为什么必须有 Node：server.py 的 TSTranspileMiddleware 用 Node 自带的
module.stripTypeScriptTypes 把 /static 下的 .ts 实时转成 JS。Node 缺失或版本过低时
server.py 会静默退回「原样直服」，浏览器把 TS 源码当 JS 跑 → 模块图整个崩掉 →
页面永远停在「连接中…」，而服务端一行错都不报，新手根本查不出这种故障。

装到 %LOCALAPPDATA%\\dabai\\node 而不是系统目录：不需要管理员权限（官方 MSI 默认
装到 Program Files，会弹 UAC），也不覆盖用户自己装的 Node —— 版本不合用时只在私有
目录补一份。

用法（dabai.bat 调用）：
    python tools/install_node.py --ensure   缺 / 版本低就下载安装
    python tools/install_node.py --check    只报告状态，不下载

--ensure 模式下：进度写 stderr（给人看），stdout 只留最后一行结果
（`OK <node.exe 路径>` 或 `FAIL <原因>`），批处理用 for /f 取最后一行。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

NODE_VERSION = "22.23.2"

# module.stripTypeScriptTypes 的下限（Node 文档 Added in: v22.13.0）。
# 低于它 Node 认不出这个 API，转译必失败 —— 所以「装了 Node」不等于「够用」。
MIN_VERSION = (22, 13)
# 先国内镜像再官方源：nodejs.org 在国内常只有几十 KB/s，35 MB 要下十几分钟。
SOURCES = [
    f"https://registry.npmmirror.com/-/binary/node/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
    f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
]

CHUNK = 256 * 1024
MIN_ZIP_BYTES = 1024 * 1024


def eprint(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`v22.23.2` / `22.13.0\r\n` / `v24.0.0-rc.1` → (22, 23, 2)；认不出返回 None。"""
    parts = text.strip().lstrip("vV").split("-", 1)[0].split(".")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_supported(ver: tuple[int, int, int] | None) -> bool:
    """按 Node 文档的 Added in 判定：v22.13.0 / v23.2.0。

    不能写成 `(major, minor) >= (22, 13)` —— 元组比较会把 23.0/23.1 当成
    「比 22.13 新」而放行，可它们其实还没这个 API。
    """
    if ver is None:
        return False
    major, minor = ver[0], ver[1]
    if major < 22:
        return False
    if major == 22:
        return minor >= 13
    if major == 23:
        return minor >= 2
    return True


def fmt(ver: tuple[int, int, int] | None) -> str:
    return "v" + ".".join(str(x) for x in ver) if ver else "未知"


def private_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "dabai" / "node"


def private_exe() -> Path:
    return private_dir() / ("node.exe" if os.name == "nt" else "node")


def node_version(exe) -> tuple[int, int, int] | None:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return parse_version(out.stdout or out.stderr)


def find_usable() -> tuple[Path | None, tuple[int, int, int] | None]:
    """返回 (可用 node 路径, 版本)。私有目录优先 —— 那份版本由我们保证够新。"""
    candidates = [private_exe(), shutil.which("node")]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand)
        if not path.is_file():
            continue
        ver = node_version(path)
        if is_supported(ver):
            return path, ver
    return None, None


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "dabai-install-node"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        next_pct = 10
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total:
                pct = got * 100 // total
                if pct >= next_pct:
                    eprint(f"    已下载 {pct}%（{got // 1048576} MB / {total // 1048576} MB）")
                    next_pct = (pct // 10 + 1) * 10
    size = dest.stat().st_size
    if size < MIN_ZIP_BYTES:
        raise RuntimeError(f"下载内容只有 {size} 字节，疑似被网关/运营商拦截页替换")


def extract_strip_top(zip_path: Path, dest: Path) -> int:
    """解压并剥掉顶层目录（node-v22.23.2-win-x64/），让 node.exe 直接落在 dest 下。

    返回跳过的文件数：Node 的 zip 里带 npm 的完整 node_modules，个别路径会超过
    Windows 260 字符上限（除非注册表开了长路径）。跳过它们不影响转译功能，
    但要让调用方知道发生过。
    """
    root = dest.resolve()
    skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if "/" not in name:
                continue
            inner = name.split("/", 1)[1]
            if not inner:
                continue
            target = (root / inner).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"压缩包内含越界路径：{info.filename}")
            try:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
            except OSError:
                skipped += 1
    return skipped


def is_windows() -> bool:
    """抽成函数是为了能在非 Windows 上测 do_ensure 的其它分支。"""
    return os.name == "nt"


def do_ensure() -> int:
    if not is_windows():
        print("FAIL 本脚本只负责 Windows；Linux/macOS 请用系统包管理器装 Node 22.13+（apt install nodejs 或 nvm）")
        return 1
    exe, ver = find_usable()
    if exe:
        eprint(f"  Node.js 已就绪：{exe}（{fmt(ver)}）")
        print(f"OK {exe}")
        return 0

    want = f"{MIN_VERSION[0]}.{MIN_VERSION[1]}"
    eprint(f"  未找到可用的 Node.js（需要 {want}+），开始自动安装 {NODE_VERSION} ...")
    eprint("  下载约 35 MB，请勿关闭窗口。")

    tmp = Path(tempfile.mkdtemp(prefix="dabai-node-"))
    zip_path = tmp / "node.zip"
    last_err = ""
    for url in SOURCES:
        try:
            eprint(f"  下载源：{url}")
            download(url, zip_path)
            break
        except Exception as exc:
            last_err = f"{exc.__class__.__name__}: {exc}"
            eprint(f"    [失败] {last_err}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"FAIL 所有下载源都失败（最后错误：{last_err}）")
        return 1

    dest = private_dir()
    try:
        eprint(f"  解压到 {dest} ...")
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        skipped = extract_strip_top(zip_path, dest)
    except Exception as exc:
        print(f"FAIL 解压失败：{exc.__class__.__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if skipped:
        eprint(f"  提示：{skipped} 个文件因路径过长跳过（属 npm 内部文件，不影响转译）")

    exe = private_exe()
    ver = node_version(exe) if exe.is_file() else None
    if not is_supported(ver):
        print(f"FAIL 装完仍不可用：{exe} 版本={fmt(ver)}")
        return 1
    eprint(f"  安装完成：{exe}（{fmt(ver)}）")
    print(f"OK {exe}")
    return 0


def do_check() -> int:
    exe, ver = find_usable()
    if exe:
        print(f"[OK] Node.js {fmt(ver)} — {exe}")
        return 0
    print(f"[X] 未找到可用的 Node.js（需要 {MIN_VERSION[0]}.{MIN_VERSION[1]}+）")
    for cand in (private_exe(), shutil.which("node")):
        if cand and Path(cand).is_file():
            print(f"    {cand} 版本过低：{fmt(node_version(cand))}")
    if is_windows():
        print("    网页会永远卡在「连接中…」。跑 dabai.bat 会自动装一份到用户目录。")
    else:
        print("    网页会永远卡在「连接中…」。装法：apt install nodejs（需 22.13+）"
              "或 nvm install 22")
    return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Windows 下自动安装 Node.js（前端 .ts 转译依赖）")
    ap.add_argument("--ensure", action="store_true", help="缺 / 版本低就下载安装")
    ap.add_argument("--check", action="store_true", help="只报告状态，不下载")
    args = ap.parse_args()
    if args.check:
        return do_check()
    return do_ensure()


if __name__ == "__main__":
    sys.exit(main())
