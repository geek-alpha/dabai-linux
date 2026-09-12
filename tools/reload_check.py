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
    """当前 server 进程的 PID 与启动时间（秒）。

    Linux 优先（systemctl / /proc），Windows 兜底（powershell）。
    探测失败返回 (None, None) —— 调用方必须报 UNKNOWN，绝不能当成 [OK]。
    """
    if sys.platform.startswith("linux"):
        pid, started = _proc_start_linux()
        if pid:
            return pid, started
    return _proc_start_windows()


def _proc_start_linux() -> tuple:
    pid = None
    try:
        out = subprocess.run(
            ["systemctl", "show", "myservice.service", "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10)
        v = (out.stdout or "").strip()
        if v.isdigit() and int(v) > 0:
            pid = int(v)
    except Exception:
        pid = None
    if not pid:  # 兜底：扫 /proc 找 server.py
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                try:
                    with open("/proc/%s/cmdline" % d, "rb") as f:
                        cmd = f.read().decode("utf-8", "ignore")
                except Exception:
                    continue
                if "server.py" in cmd:
                    pid = int(d)
                    break
        except Exception:
            pid = None
    if not pid:
        return None, None
    try:
        with open("/proc/%d/stat" % pid, "r") as f:
            stat = f.read()
        fields = stat[stat.rfind(")") + 2:].split()
        starttime = int(fields[19])  # 第 22 字段（starttime，clock ticks）
        hz = os.sysconf("SC_CLK_TCK") or 100
        btime = 0
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("btime"):
                    btime = int(line.split()[1])
                    break
        if btime:
            return pid, btime + starttime / float(hz)
        with open("/proc/uptime", "r") as f:
            uptime = float(f.read().split()[0])
        return pid, time.time() - uptime + starttime / float(hz)
    except Exception:
        return None, None


def _proc_start_windows() -> tuple:
    """Windows 兜底（原实现保留）。"""
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
    if not started:
        # 关键：探测失败 ≠ 已生效。旧版在这里静默放过，永远打印 [OK]，
        # 把“探测不到”伪装成“没问题”（实测踩过：agent.py 改完未生效却报 OK）。
        print("[?] 探测不到运行中进程 —— 无法判定改动是否生效（这不是 [OK]）。")
        print("    按修改时间列出的核心文件，请人工核对：")
        for p in sorted(files, key=lambda x: -x.stat().st_mtime)[:10]:
            print("   %-24s %s" % (p.name,
                                    time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(p.stat().st_mtime))))
        return 2
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
