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


LOADED_STATE = BASE / "data" / "hot_reload_state.json"


def _loaded_state() -> dict:
    """读 hot_reload 落盘的「已加载快照」（hot_reload._dump_loaded_state 写）。

    为什么不能只看进程启动时间：core_autorestart=true 时重启走 os.execv 自替换，
    PID 与 /proc/<pid>/stat 的 starttime 都冻结在进程创建时刻（实测 execv 前后
    19284→19284、ticks 1214348→1214348），于是「文件比进程新」在每次自动重启后
    依然成立，永远误报未生效。
    """
    try:
        with open(LOADED_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _restart_check_report() -> str:
    """跑一次重启体检（tools/restart_server.sh --check），返回输出末尾。

    为什么由本工具来跑：reload_check 只知道「有改动没生效」，不知道「为什么
    没生效」——单元状态、端口、守护日志都在重启脚本的体检里。发现问题的工具
    顺手把证据留下，比让人再手敲一条命令可靠。

    防递归：重启脚本内部会回调本工具，所以给它 DABAI_NO_RECHECK=1，并把报告
    写到另一个文件，不覆盖上次真重启的报告。
    """
    script = BASE / "tools" / "restart_server.sh"
    if not script.is_file():
        return ""
    env = dict(os.environ)
    env["DABAI_NO_RECHECK"] = "1"
    env["DABAI_REPORT"] = str(BASE / "data" / "restart_check_report.txt")
    try:
        out = subprocess.run(["bash", str(script), "--check"],
                             capture_output=True, text=True, timeout=60, env=env)
    except Exception as e:
        return "体检脚本执行失败: %s" % e
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    return text[-2000:] if len(text) > 2000 else text


def main(as_json=False):
    pid, started = _proc_start()
    auto = _autorestart_enabled()
    files = _scan_core()
    state = _loaded_state()
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
        "state_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(state.get("at")))
                     if state and state.get("at") else None),
    }
    core_state = (state.get("core") or {}) if state else {}
    if core_state and pid and state.get("pid") and int(state["pid"]) != int(pid):
        # 快照不是本进程写的：被测试夹具或别的进程覆盖过。此时指纹比对会产出一整片假
        # 警报（实测 2026-09-14 06:40：全套测试把 {"core": {"/fake/core_x.py": [1,10]}}
        # 写进生产快照，本工具立刻报「44 个核心文件未生效」）。宁可降级判据并告警，
        # 也不拿别人的快照当权威。
        print("[!] 已加载快照的 pid=%s ≠ 运行中进程 pid=%s —— 快照不是本进程写的，"
              "降级用进程启动时间判定（自动重启后可能误报；下次重启会重建快照）。"
              % (state.get("pid"), pid))
        core_state = {}
    res["judge"] = "loaded_state" if core_state else "proc_start"
    if core_state:
        # 首选判据：守护落盘的已加载快照——直接比 mtime_ns+size，与重启方式无关
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            if core_state.get(str(p)) != [st.st_mtime_ns, st.st_size]:
                res["stale"].append({
                    "file": p.name,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "ahead_sec": int(st.st_mtime - (state.get("at") or 0)),
                })
    elif started:
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
    # 改动没生效时顺手体检：谁发现没生效，谁留下证据（报告落盘）
    res["restart_check"] = _restart_check_report() if res["stale"] else ""
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("运行中进程 PID %s，启动于 %s" % (res["pid"], res["started_at"]))
    print("自动重启（harness.core_autorestart）：%s"
          % ("开启" if auto else "关闭 ← 核心改动不会自动生效"))
    if res["judge"] == "loaded_state":
        print("判据：已加载快照（%s，落盘于 %s）"
              % (LOADED_STATE.name, res.get("state_at") or "未知"))
    else:
        print("判据：进程启动时间 —— 已加载快照缺失（守护未重启或未开启热重载）；"
              "该判据在自动重启后会误报")
    if not started and res["judge"] != "loaded_state":
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
        print("[OK] 运行中进程加载的就是当前磁盘版本")
        return 0
    print("[!] 有 %d 个核心文件的改动还没进运行中的进程：" % res["stale_count"])
    _ref = "已加载快照" if res["judge"] == "loaded_state" else "进程启动"
    for it in res["stale"][:15]:
        print("   %-24s 改于 %s（比%s晚 %ds）"
              % (it["file"], it["mtime"], _ref, it["ahead_sec"]))
    if not auto:
        print("\n原因：core_autorestart=false —— 需要手动重启 server.py 才会生效。")
    diag = res.get("restart_check")
    if diag:
        print("\n--- 重启体检（%s）---" % (BASE / "data" / "restart_check_report.txt"))
        print(diag)
    return 1


if __name__ == "__main__":
    sys.exit(main("--json" in sys.argv[1:]))
