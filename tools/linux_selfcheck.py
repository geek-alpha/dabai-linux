#!/usr/bin/env python3
"""大白 Linux/macOS 环境自检 —— 启动前跑一遍，把"运行期才会炸的问题"提前暴露。

用法：
    python3 tools/linux_selfcheck.py          # 人类可读报告
    python3 tools/linux_selfcheck.py --json   # 机器可读（CI 用）

退出码：0 = 可启动（可能有警告）；1 = 存在阻塞项。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_rows: list[tuple[str, str, str]] = []


def check(name: str, level: str, detail: str = "") -> None:
    _rows.append((name, level, detail))


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def main() -> int:
    # 1) Python 版本
    v = sys.version_info
    if v >= (3, 10):
        check("Python 版本", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        check("Python 版本", FAIL, f"{v.major}.{v.minor} 过低，需要 3.10+")

    # 2) 平台识别 + 兼容层
    try:
        import platform_compat as pc
        check("platform_compat 兼容层", OK,
              f"平台={sys.platform} Windows={pc.IS_WINDOWS} Linux={pc.IS_LINUX} macOS={pc.IS_MACOS}")
    except Exception as e:
        pc = None
        check("platform_compat 兼容层", FAIL, f"导入失败：{e.__class__.__name__}: {e}")

    # 3) 关键文件
    for rel in ("server.py", "codex_runner.py", "settings.json"):
        p = ROOT / rel
        check(f"文件 {rel}", OK if p.is_file() else FAIL, "" if p.is_file() else "缺失")
    web_index = ROOT / "web" / "index.html"
    check("前端 web/index.html", OK if web_index.is_file() else WARN,
          "" if web_index.is_file() else "缺失（网页界面不可用）")

    # 4) 关键依赖
    core = ["fastapi", "uvicorn", "aiohttp", "requests", "starlette", "numpy"]
    missing = [m for m in core if not _has(m)]
    check("核心依赖", FAIL if missing else OK, "缺少：" + ", ".join(missing) if missing else "")

    optional = ["PIL", "mss", "edge_tts", "playwright", "yt_dlp", "netifaces"]
    missing_opt = [m for m in optional if not _has(m)]
    check("可选依赖", WARN if missing_opt else OK,
          "未安装（对应能力降级）：" + ", ".join(missing_opt) if missing_opt else "")

    # 5) 外部工具
    for tool, why in (("ffmpeg", "音视频处理"), ("git", "代码工程技能"), ("rg", "快速检索（可回退）")):
        p = shutil.which(tool)
        check(f"外部工具 {tool}", OK if p else WARN, p or f"未找到（{why} 受影响）")
    blender = shutil.which("blender") or os.environ.get("DABAI_BLENDER", "")
    check("Blender（模型转换）", OK if blender else WARN,
          blender or "未找到（PMX→VRM 技能不可用，可设 DABAI_BLENDER）")
    chrome = os.environ.get("DABAI_CHROME", "")
    if not chrome:
        for name in ("google-chrome", "chromium", "chromium-browser"):
            if shutil.which(name):
                chrome = shutil.which(name)
                break
    check("Chrome/Chromium（网页深挖）", OK if chrome else WARN,
          chrome or "未找到（JS 页面抓取降级为 requests）")

    # 6) 图形会话（截屏/声音需要）
    disp = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    check("图形会话", OK if disp else WARN,
          f"DISPLAY={disp}" if disp else "无 DISPLAY/WAYLAND_DISPLAY（截屏不可用）")
    in_container = Path("/.dockerenv").exists()
    if not in_container and Path("/proc/1/cgroup").is_file():
        try:
            cg = Path("/proc/1/cgroup").read_text(errors="replace")
            in_container = any(k in cg for k in ("docker", "kubepods", "containerd", "lxc"))
        except OSError:
            pass
    kv = ""
    if Path("/proc/version").is_file():
        try:
            kv = Path("/proc/version").read_text(errors="replace").lower()
        except OSError:
            pass
    if in_container:
        check("运行环境", OK, "容器环境（无图形/声音属正常）")
    elif "microsoft" in kv:
        check("运行环境", OK, "WSL 检测到（需要 WSLg 才有图形/声音）")

    # 7) 端口占用
    port = 3080
    try:
        raw = (ROOT / "settings.json").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'"port"\s*:\s*(\d+)', raw)
        if m:
            port = int(m.group(1))
    except OSError:
        pass
    s = socket.socket()
    s.settimeout(1)
    try:
        s.bind(("127.0.0.1", port))
        check(f"端口 {port} 可绑定", OK, "")
    except OSError as e:
        check(f"端口 {port} 可绑定", WARN, f"已被占用：{e}（若服务已在运行属正常）")
    finally:
        s.close()

    # 8) 兼容层关键能力实跑（不是"能 import"就算过）
    if pc is not None:
        try:
            procs = pc.list_processes()
            check("进程清单读取", OK if procs else WARN, f"{len(procs)} 个进程")
        except Exception as e:
            check("进程清单读取", WARN, f"{e.__class__.__name__}: {e}")
        try:
            ports = pc.list_listening_ports()
            check("监听端口读取", OK, f"{len(ports)} 条")
        except Exception as e:
            check("监听端口读取", WARN, f"{e.__class__.__name__}: {e}")
        try:
            du = pc.disk_free(str(ROOT))
            check("磁盘空间读取", OK if du else WARN,
                  f"剩余 {du[1] / 2**30:.1f} GB" if du else "失败")
        except Exception as e:
            check("磁盘空间读取", WARN, f"{e.__class__.__name__}: {e}")
        try:
            import subprocess
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                                 **pc.spawn_kwargs(new_group=True))
            ok, reason = pc.terminate_tree(p.pid, timeout=5)
            check("整树终止进程", OK if ok else FAIL, reason)
        except Exception as e:
            check("整树终止进程", FAIL, f"{e.__class__.__name__}: {e}")

    # ---- 输出 ----
    if "--json" in sys.argv:
        print(json.dumps([{"item": n, "level": l, "detail": d} for n, l, d in _rows],
                         ensure_ascii=False, indent=2))
    else:
        print(f"大白环境自检 —— {sys.platform} / Python {v.major}.{v.minor}.{v.micro}")
        print(f"项目根：{ROOT}\n")
        width = max(len(n) for n, _, _ in _rows)
        for name, level, detail in _rows:
            mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[level]
            print(f"{mark} {name.ljust(width)}  {detail}")
        fails = [r for r in _rows if r[1] == FAIL]
        warns = [r for r in _rows if r[1] == WARN]
        print(f"\n合计：{len(_rows)} 项检查，{len(fails)} 项阻塞，{len(warns)} 项警告")
        if fails:
            print("阻塞项需先解决：")
            for name, _, detail in fails:
                print(f"  - {name}：{detail}")
    return 1 if any(r[1] == FAIL for r in _rows) else 0


if __name__ == "__main__":
    sys.exit(main())
