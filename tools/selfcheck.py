#!/usr/bin/env python3
"""大白环境自检（Windows / Linux / macOS）—— 启动前跑一遍，把"运行期才会炸的问题"提前暴露。

用法：
    python3 tools/selfcheck.py          # 人类可读报告
    python3 tools/selfcheck.py --json   # 机器可读（CI 用）

退出码：0 = 可启动（可能有警告）；1 = 存在阻塞项。

原名 linux_selfcheck.py；Windows 要跑同一套检查，故去掉平台前缀。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同目录的 check_deps.py

# 独立进程也要同步新旧环境变量名：用户设了 DABAI_BLENDER，下面的 DABAI_* 旧读点
# 也得看到同一个值（双向同步在 env_compat 里）。
import install_node  # 同目录：Node 版本下限的单一来源（前端转译依赖它）


IS_WINDOWS = sys.platform == "win32"

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
    for rel in ("server.py", "codex_runner.py"):
        p = ROOT / rel
        check(f"文件 {rel}", OK if p.is_file() else FAIL, "" if p.is_file() else "缺失")
    # settings.json 不入仓（含 api_key，在 .gitignore 里），全新 clone 只有 example。
    # server.py 首次启动会自动生成一份；这里也顺手补，别报一个必然会消失的阻塞项。
    settings = ROOT / "settings.json"
    if settings.is_file():
        check("文件 settings.json", OK, "")
    else:
        example = ROOT / "settings.example.json"
        if example.is_file():
            settings.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            check("文件 settings.json", OK, "已从 settings.example.json 生成（首次，去设置页填 API Key）")
        else:
            check("文件 settings.json", FAIL, "缺失，且找不到 settings.example.json")
    web_index = ROOT / "web" / "index.html"
    check("前端 web/index.html", OK if web_index.is_file() else WARN,
          "" if web_index.is_file() else "缺失（网页界面不可用）")

    # 4) 关键依赖
    # 清单来自 tools/check_deps.py（与 requirements-core.txt 同源），不在这里再抄一份——
    # 抄一份的下场就是漏掉 uvloop/httptools/python-multipart 这类「一 import 就崩」的。
    from check_deps import missing as missing_deps
    miss = missing_deps()
    check("核心依赖", FAIL if miss else OK,
          "缺少：" + ", ".join(p for _, p in miss) if miss else "")

    # 缺了只降级不阻塞：PDF 附件 / 手机配对码 / 证书生成后端
    optional = ["PIL", "mss", "playwright", "yt_dlp", "netifaces",
                "cryptography", "pypdf", "qrcode"]
    missing_opt = [m for m in optional if not _has(m)]
    check("可选依赖", WARN if missing_opt else OK,
          "未安装（对应能力降级）：" + ", ".join(missing_opt) if missing_opt else "")

    # 5) 外部工具
    for tool, why in (("ffmpeg", "音视频处理"), ("git", "代码工程技能"), ("rg", "快速检索（可回退）")):
        p = shutil.which(tool)
        check(f"外部工具 {tool}", OK if p else WARN, p or f"未找到（{why} 受影响）")

    # 前端 .ts 靠常驻 Node worker 实时转译（server.py 的 TSTranspileMiddleware）。
    # node 缺失时服务照常起，但 /static/*.ts 会原样下发 → 浏览器语法报错 → 页面永远
    # 停在「连接中…」，而服务端一行错都不报。所以必须在自检里点出来。
    node = shutil.which("node")
    if not node:
        check("Node.js（前端必需）", WARN,
              "未找到：网页会永远卡在「连接中…」。跑 dabai.bat 会自动装，或手动装 Node.js 22.13+ 后重启服务")
    else:
        try:
            out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
            raw = (out.stdout or out.stderr).strip()
            ok = install_node.is_supported(install_node.parse_version(raw))
            check("Node.js（前端必需）", OK if ok else WARN,
                  raw.lstrip("v") if ok else f"{raw} 过低：需要 22.13+ / 23.2+（module.stripTypeScriptTypes 的 Added in 版本），否则网页卡在「连接中…」")
        except Exception as e:
            check("Node.js（前端必需）", WARN, f"版本探测失败：{e.__class__.__name__}: {e}")
    blender = shutil.which("blender") or os.environ.get("DABAI_BLENDER", "")
    if not blender and IS_WINDOWS:
        base = Path(r"C:\Program Files\Blender Foundation")
        if base.is_dir():
            hits = sorted(base.glob("Blender */blender.exe"), reverse=True)
            if hits:
                blender = str(hits[0])
    check("Blender（模型转换）", OK if blender else WARN,
          blender or "未找到（PMX→VRM 技能不可用，可设 DABAI_BLENDER）")
    chrome = os.environ.get("DABAI_CHROME", "")
    if not chrome:
        for name in ("google-chrome", "chromium", "chromium-browser"):
            if shutil.which(name):
                chrome = shutil.which(name)
                break
    if not chrome and IS_WINDOWS:
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
            if Path(p).is_file():
                chrome = p
                break
    check("Chrome/Chromium（网页深挖）", OK if chrome else WARN,
          chrome or "未找到（JS 页面抓取降级为 requests）")

    # 6) 图形会话（截屏/声音需要）—— Windows 没有 DISPLAY 变量，桌面会话恒存在
    if IS_WINDOWS:
        check("图形会话", OK, "Windows 桌面会话")
    else:
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
