"""生效自检：有哪些核心改动还没进运行中的进程？

为什么必须有这个工具：harness.core_autorestart=false 时，改核心代码只记日志、
不重启——于是「改完了」和「改生效了」是两回事，而角色很容易把两者当成一件事
（实测踩过：连续 3 轮改 agent.py / memory.py 并声称「重启后生效」，实际进程
一直是同一个，改动全没进内存，验证数据自然毫无变化）。

用法：
  python tools/reload_check.py          # 列出未生效的核心改动
  python tools/reload_check.py --json   # 机器可读

注：输出一律用 ASCII 标记（[OK]/[!]）—— Windows 控制台默认 GBK，
    打印 ✅/⚠ 这类符号会直接 UnicodeEncodeError 把脚本搞崩。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _scan_core() -> list:
    """与 hot_reload._scan_core 保持一致：根目录 *.py + harness/*.py。"""
    files = [p for p in BASE.glob("*.py") if p.name != "__init__.py"]
    h = BASE / "harness"
    if h.is_dir():
        files += list(h.glob("*.py"))
    return files


def _proc_start() -> tuple:
    """当前 python server.py 进程的 PID 与启动时间（秒）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate.ToString('o'))\" }"],
            capture_output=True, text=True, timeout=30)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            pid, ts = line.split("|", 1)
            try:
                # ISO 8601 带本地时区 → 转 epoch
                from datetime import datetime
                dt = datetime.fromisoformat(ts.strip())
                return int(pid), dt.timestamp()
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def _autorestart_enabled() -> bool:
    try:
        with open(BASE / "settings.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(((cfg or {}).get("harness") or {}).get("core_autorestart", False))
    except Exception:
        return False


def main(as_json=False):
    pid, started = _proc_start()
    auto = _autorestart_enabled()
    files = _scan_core()
    newest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    res = {
        "pid": pid,
        "autorestart": auto,
        "started_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
                       if started else None),
        "newest_file": (newest.name if newest else None),
        "newest_mtime": (time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(newest.stat().st_mtime))
                         if newest else None),
        "stale": [],
    }
    if started:
        for p in files:
            m = p.stat().st_mtime
            if m > started + 1:  # 1 秒容差
                res["stale"].append({
                    "file": p.name,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m)),
                    "ahead_sec": int(m - started),
                })
    res["stale"].sort(key=lambda x: -x["ahead_sec"])
    res["stale_count"] = len(res["stale"])
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("运行中进程 PID %s，启动于 %s" % (res["pid"], res["started_at"]))
    print("自动重启（harness.core_autorestart）：%s"
          % ("开启" if auto else "关闭 ← 核心改动不会自动生效"))
    if not res["stale"]:
        print("[OK] 所有核心改动都已生效（没有比进程启动时间更新的核心文件）")
        return 0
    print("[!] 有 %d 个核心文件的改动还没进运行中的进程：" % res["stale_count"])
    for it in res["stale"][:15]:
        print("   %-24s 改于 %s（比进程启动晚 %ds）"
              % (it["file"], it["mtime"], it["ahead_sec"]))
    if not auto:
        print("\n原因：core_autorestart=false —— 需要手动重启 server.py 才会生效。")
    return 1


if __name__ == "__main__":
    sys.exit(main("--json" in sys.argv[1:]))
