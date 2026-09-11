#!/usr/bin/env python3
"""系统支配层 —— 进程 / 网络 / 存储 / 安全审计。

补齐从「感知」到「支配」的鸿沟：原先 linux_native 只能读状态 + 管 systemd 服务，
现在还能看进程、调进程调度、审网络暴露面、查磁盘去向、扫安全弱点。

设计原则（与 senses_impl 对齐）：
    1. 只读优先 —— list / detail / overview / audit 全只读；只有 signal / tune 是写操作。
    2. 写操作有闸门 —— 不许杀 init、大白自己、自己的祖先链、内核线程。
       杀祖先链 = 回复断在半路（这个坑真踩过）。
    3. 数据源失效只标记该项，不整体失败。
    4. 读不到就说读不到，绝不返回空串假装「一切正常」。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

# ---------------------------------------------------------------- 公共

_PS_FIELDS = "pid,ppid,user,pcpu,pmem,rss,stat,etimes,comm"


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """跑外部命令，返回 (rc, 输出)。永不抛异常 —— 支配层不因单点失败整体崩。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）：{' '.join(cmd[:3])}"
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001
        return default


def _human_kb(kb: float) -> str:
    for unit in ("KB", "MB", "GB", "TB"):
        if abs(kb) < 1024:
            return f"{kb:.0f}{unit}" if unit == "KB" else f"{kb:.1f}{unit}"
        kb /= 1024
    return f"{kb:.1f}PB"


# ---------------------------------------------------------------- 进程支配

# 绝不能杀的进程（名字级兜底；PID 1 / 自身 / 祖先链另在 _signal_deny 里判）
_CRITICAL_NAMES = {
    "systemd", "init", "kthreadd", "myservice", "server.py",
    "sshd", "dbus-daemon", "systemd-journald", "systemd-logind",
    "nginx", "cloudflared",
}


def _ancestors(pid: int | None = None) -> set[int]:
    """返回 pid（默认当前进程）的祖先链，含自己。

    杀这些 PID 会连坐到大白 —— 尤其是「大白从自己的 shell 里起进程」的场景，
    杀了祖先就等于自杀。这个坑在 systemd 部署脚本里真踩过一次。
    """
    chain: set[int] = set()
    cur = pid if pid is not None else os.getpid()
    for _ in range(64):
        if cur <= 1:
            break
        chain.add(cur)
        stat = _read(f"/proc/{cur}/stat")
        if not stat or ")" not in stat:
            break
        # comm 可能含空格与括号，必须从最后一个 ')' 之后切
        try:
            fields = stat[stat.rindex(")") + 2:].split()
            ppid = int(fields[1])
        except (ValueError, IndexError):
            break
        if ppid in chain:
            break
        cur = ppid
    return chain


def _proc_info(pid: int) -> dict:
    """从 /proc/<pid> 取单进程详情。取不到的项标 None，不猜。"""
    base = f"/proc/{pid}"
    info: dict = {"pid": pid, "exists": os.path.isdir(base)}
    if not info["exists"]:
        return info

    info["comm"] = _read(f"{base}/comm")
    stat = _read(f"{base}/stat")
    if stat and ")" in stat:
        try:
            fields = stat[stat.rindex(")") + 2:].split()
            info["state"] = fields[0]
            info["ppid"] = int(fields[1])
            info["utime_ticks"] = int(fields[11])
            info["stime_ticks"] = int(fields[12])
            info["threads"] = int(fields[17])
            info["rss_pages"] = int(fields[21])
        except (ValueError, IndexError):
            pass

    try:
        info["cmdline"] = " ".join(
            open(f"{base}/cmdline", "rb").read().decode("utf-8", "replace").split("\0")
        ).strip()
    except Exception:  # noqa: BLE001
        info["cmdline"] = ""

    try:
        info["exe"] = os.readlink(f"{base}/exe")
    except OSError as e:
        info["exe"] = f"(读不到：{e.strerror or e})"
    try:
        info["cwd"] = os.readlink(f"{base}/cwd")
    except OSError:
        info["cwd"] = None
    try:
        info["user"] = _read(f"{base}/status").split("Uid:")[1].split()[0]
    except Exception:  # noqa: BLE001
        info["user"] = None
    try:
        info["fd_count"] = len(os.listdir(f"{base}/fd"))
    except Exception:  # noqa: BLE001
        info["fd_count"] = None

    # 内核线程：无 cmdline 且 exe 读不到
    info["kernel_thread"] = (
        not info.get("cmdline") and str(info.get("exe", "")).startswith("(读不到")
    )
    return info


def _is_kernel_thread(pid: int) -> bool:
    """内核线程判据：PPID == 2（kthreadd）或没有 cmdline 也没有 exe。"""
    info = _proc_info(pid)
    return bool(info.get("kernel_thread")) or info.get("ppid") == 2


def process_list(sort_by: str = "cpu", limit: int = 15) -> str:
    """进程排行。sort_by: cpu / mem / rss / time。"""
    sort_by = (sort_by or "cpu").strip().lower()
    key = {
        "cpu": "-pcpu", "mem": "-pmem", "rss": "-rss", "time": "-etimes",
    }.get(sort_by, "-pcpu")
    try:
        limit = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        limit = 15

    rc, out = _run([
        "ps", "-eo", _PS_FIELDS, f"--sort={key}", "--no-headers",
    ])
    if rc != 0:
        return f"✗ 读进程列表失败（ps rc={rc}）：{out}"

    rows = []
    for line in out.splitlines()[:limit]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        pid, ppid, user, pcpu, pmem, rss, stat, etimes, comm = parts
        try:
            rss_kb = int(rss)
        except ValueError:
            rss_kb = 0
        rows.append({
            "pid": pid, "ppid": ppid, "user": user, "pcpu": pcpu,
            "pmem": pmem, "rss_kb": rss_kb, "stat": stat,
            "etimes": etimes, "comm": comm.strip(),
        })

    if not rows:
        return f"✗ ps 输出无法解析（原始前 200 字）：{out[:200]}"

    total = _read("/proc/loadavg").split()[:3]
    lines = [
        f"进程排行（按 {sort_by} 降序，共 {len(rows)} 条 / 系统共 "
        f"{len([d for d in os.listdir('/proc') if d.isdigit()])} 个进程）",
        f"  负载 {', '.join(total)}   内存余量见 linux_senses",
        "",
    ]
    for r in rows:
        try:
            up = int(r["etimes"])
            age = f"{up // 86400}d" if up >= 86400 else (
                f"{up // 3600}h" if up >= 3600 else f"{up // 60}m")
        except ValueError:
            age = "?"
        lines.append(
            f"  {r['pid']:>6}  {r['comm'][:24]:<24} CPU{r['pcpu']:>6}%  "
            f"MEM{r['pmem']:>5}%  {_human_kb(r['rss_kb']):>8}  {r['stat']:<5} 存活{age}"
        )
    lines.append("")
    lines.append("  提示：detail 看详情 / signal 发信号 / tune 调优先级与 CPU 亲和性")
    return "\n".join(lines)


def process_detail(pid: int) -> str:
    """单进程详情。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return f"✗ pid 必须是数字，收到 {pid!r}"

    info = _proc_info(pid)
    if not info.get("exists"):
        return f"✗ PID {pid} 不存在"

    cmd = info.get("cmdline") or info.get("comm") or "?"
    lines = [
        f"进程 {pid}：{info.get('comm') or '?'}",
        f"  命令行   {cmd[:300]}{'…' if len(cmd) > 300 else ''}",
        f"  状态     {info.get('state', '?')}   父进程 {info.get('ppid', '?')}   "
        f"线程 {info.get('threads', '?')}",
        f"  可执行   {info.get('exe', '?')}",
        f"  cwd      {info.get('cwd') or '(读不到，可能是内核线程或无权限)'}",
        f"  打开 fd  {info.get('fd_count') if info.get('fd_count') is not None else '(无权限)'}",
    ]
    if info.get("rss_pages"):
        lines.append(f"  常驻内存 {_human_kb(info['rss_pages'] * 4)}")

    try:
        aff = sorted(os.sched_getaffinity(pid))
        lines.append(f"  CPU 亲和 {'/'.join(map(str, aff))}（共 {os.cpu_count()} 核）")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  CPU 亲和 (读不到：{type(e).__name__})")

    if info.get("kernel_thread"):
        lines.append("  ⚠ 内核线程 —— 不可杀、不可调优先级")
    if pid == 1:
        lines.append("  ⚠ PID 1（init）—— 杀它会立刻整机崩溃")
    if pid in _ancestors():
        lines.append("  ⚠ 这是大白的祖先链成员 —— 杀它会连坐到自己")
    return "\n".join(lines)


_SIGNALS = {
    "TERM": 15, "KILL": 9, "HUP": 1, "INT": 2,
    "USR1": 10, "USR2": 12, "CONT": 18, "STOP": 19,
}


def _signal_deny(pid: int, sig: str) -> str | None:
    """信号闸门：返回 None 放行，否则拒绝理由。"""
    if pid == 1:
        return "✗ 拒绝向 PID 1（init/systemd）发信号 —— 杀它等于立刻整机崩溃"
    if pid == os.getpid():
        return "✗ 拒绝向大白自己（当前进程）发信号 —— 会直接断掉这次回复"
    if pid in _ancestors():
        return (
            f"✗ 拒绝向 PID {pid} 发信号 —— 它是大白的祖先链成员。\n"
            f"  杀掉它会连坐到自己（回复断在半路、任务半途而废）。"
        )
    if _is_kernel_thread(pid):
        return f"✗ 拒绝向 PID {pid} 发信号 —— 内核线程由内核管理，用户态动不了"
    if sig == "KILL":
        info = _proc_info(pid)
        name = (info.get("comm") or "").strip()
        if name in _CRITICAL_NAMES:
            return (
                f"✗ 拒绝 SIGKILL 关键进程「{name}」—— 它承载着大白本身或对外服务。\n"
                f"  确实要停，用 linux_service 停对应的 systemd unit（有自愈保护）。"
            )
    return None


def process_signal(pid: int, sig: str = "TERM") -> str:
    """向进程发信号。默认 TERM（可被捕获、优雅退出）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return f"✗ pid 必须是数字，收到 {pid!r}"

    sig = (sig or "TERM").strip().upper()
    if sig not in _SIGNALS:
        return f"✗ 不支持的信号 {sig}（可用：{', '.join(sorted(_SIGNALS))}）"

    deny = _signal_deny(pid, sig)
    if deny:
        return deny

    if not os.path.isdir(f"/proc/{pid}"):
        return f"✗ PID {pid} 不存在"

    info = _proc_info(pid)
    try:
        os.kill(pid, _SIGNALS[sig])
    except PermissionError:
        return (
            f"✗ 无权限向 PID {pid}（{info.get('comm') or '?'}）发 SIG{sig} —— "
            f"它属于别的用户，需要 root"
        )
    except ProcessLookupError:
        return f"✗ PID {pid} 已消失（竞态：刚查还在）"
    except Exception as e:  # noqa: BLE001
        return f"✗ 发 SIG{sig} 失败：{type(e).__name__}: {e}"

    # 等一小会儿看是否真的退出（TERM 可能被忽略）
    import time as _t
    for _ in range(10):
        _t.sleep(0.1)
        if not os.path.isdir(f"/proc/{pid}"):
            return f"✓ 已向 PID {pid}（{info.get('comm') or '?'}）发 SIG{sig}，进程已退出"
    return (
        f"✓ 已向 PID {pid}（{info.get('comm') or '?'}）发 SIG{sig}，但进程仍在运行\n"
        f"  可能忽略了该信号。要强制结束用 sig=KILL（不可捕获，进程没机会清理）"
    )


def process_tune(pid: int, nice: int | None = None, affinity: str | None = None) -> str:
    """调进程调度：nice 优先级（-20 最高 / 19 最低）、CPU 亲和性（如 "2,3"）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return f"✗ pid 必须是数字，收到 {pid!r}"
    if not os.path.isdir(f"/proc/{pid}"):
        return f"✗ PID {pid} 不存在"
    if _is_kernel_thread(pid):
        return f"✗ PID {pid} 是内核线程，用户态调不了调度参数"

    info = _proc_info(pid)
    name = info.get("comm") or "?"
    results = []

    if nice is not None:
        try:
            nice = int(nice)
        except (TypeError, ValueError):
            return f"✗ nice 必须是整数（-20..19），收到 {nice!r}"
        if not -20 <= nice <= 19:
            return f"✗ nice 超出范围（-20..19），收到 {nice}"
        try:
            os.setpriority(os.PRIO_PROCESS, pid, nice)
            got = os.getpriority(os.PRIO_PROCESS, pid)
            results.append(f"✓ nice → {got}")
        except PermissionError:
            results.append(
                f"✗ 调 nice 需要权限 —— 提高优先级（更小的值）要 root；"
                f"同一用户内只能降低优先级（更大的值）"
            )
        except Exception as e:  # noqa: BLE001
            results.append(f"✗ nice 失败：{type(e).__name__}: {e}")

    if affinity is not None:
        try:
            cpus = set()
            for part in str(affinity).replace(" ", "").split(","):
                if not part:
                    continue
                if "-" in part:
                    a, b = part.split("-", 1)
                    cpus.update(range(int(a), int(b) + 1))
                else:
                    cpus.add(int(part))
            if not cpus:
                return "✗ affinity 解析结果为空（示例：\"2,3\" 或 \"2-3\"）"
            total = os.cpu_count() or 1
            bad = [c for c in cpus if c < 0 or c >= total]
            if bad:
                return f"✗ CPU 编号越界 {bad} —— 本机 0..{total - 1}"
            os.sched_setaffinity(pid, cpus)
            got = sorted(os.sched_getaffinity(pid))
            results.append(f"✓ CPU 亲和 → {'/'.join(map(str, got))}")
        except PermissionError:
            results.append(f"✗ 调 CPU 亲和需要权限（同用户或 root）")
        except Exception as e:  # noqa: BLE001
            results.append(f"✗ CPU 亲和失败：{type(e).__name__}: {e}")

    if not results:
        return (
            "✗ 没有指定要调的参数。用法：\n"
            "  tune(pid, nice=10)              降低优先级，让出 CPU\n"
            "  tune(pid, affinity=\"2,3\")      只跑在 2、3 号核\n"
            "  tune(pid, nice=10, affinity=\"0-1\")  两者一起"
        )
    return f"进程 {pid}（{name}）：\n  " + "\n  ".join(results)


# ---------------------------------------------------------------- 网络支配

_SS_PROC_RE = re.compile(r'\("([^"]+)",pid=(\d+)')


def _ss(flags: str) -> tuple[int, str]:
    return _run(["ss", "-H", *flags.split()])


def _parse_ss_addr(addr: str) -> tuple[str, str]:
    """拆 `127.0.0.1:7890` / `[::]:80` / `*:22` → (host, port)。"""
    if addr.startswith("["):
        host, _, port = addr.rpartition("]:")
        return host.lstrip("["), port
    host, _, port = addr.rpartition(":")
    return host, port


def net_ports(exposure_only: bool = False) -> str:
    """监听端口清单。exposure_only=True 时只列对外（非 127.0.0.1）暴露的。"""
    rc, out = _ss("-tlnp")
    if rc != 0:
        return f"✗ 读监听端口失败（ss rc={rc}）：{out}"

    raw = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[3]
        proc_raw = " ".join(parts[5:]) if len(parts) > 5 else ""
        host, port = _parse_ss_addr(local)
        m = _SS_PROC_RE.search(proc_raw)
        raw.append({
            "port": port, "host": host,
            "name": m.group(1) if m else "",
            "pid": m.group(2) if m else "",
        })

    # 双栈监听会同时出现 `0.0.0.0:80` 与 `[::]:80` —— 同一个服务报两遍只是刷屏
    merged: dict[tuple[str, str], dict] = {}
    for r in raw:
        key = (r["port"], r["name"])
        if key in merged:
            merged[key]["hosts"].add(r["host"])
        else:
            merged[key] = {**r, "hosts": {r["host"]}}
    rows = list(merged.values())
    for r in rows:
        r["loopback"] = all(h in ("127.0.0.1", "::1", "localhost") for h in r["hosts"])

    if exposure_only:
        rows = [r for r in rows if not r["loopback"]]
    if not rows:
        return "（无匹配的监听端口）"

    rows.sort(key=lambda r: (r["loopback"], int(r["port"]) if r["port"].isdigit() else 0))

    # 已知服务的风险提示
    risky = {
        "111": "rpcbind / portmap —— 树莓派上通常用不到，暴露在外等于多一个攻击面",
        "23": "telnet —— 明文协议，绝不该开",
        "21": "FTP —— 明文协议，建议换 sftp",
        "3306": "MySQL —— 数据库不该直接对外",
        "6379": "Redis —— 无认证默认，历史上大量被挖矿",
        "27017": "MongoDB —— 同上",
        "5900": "VNC —— 远程桌面，需强密码 + 隧道",
        "445": "SMB —— 勒索软件最爱",
    }

    lines = [
        f"监听端口（{len(rows)} 个{'对外 ' if exposure_only else ''}）",
        "",
    ]
    for r in rows:
        where = "本机" if r["loopback"] else "对外"
        who = f"{r['name']}(pid {r['pid']})" if r["name"] else "(需 root 才可见进程名)"
        stack = "v4+v6" if len(r["hosts"]) > 1 else (
            "v6" if any(":" in h for h in r["hosts"]) else "v4")
        flag = "  ⚠ " + risky[r["port"]] if r["port"] in risky and not r["loopback"] else ""
        lines.append(f"  {where}  :{r['port']:<6} {stack:<5} {who}{flag}")
    return "\n".join(lines)


def net_conns(limit: int = 15) -> str:
    """活动连接概览：状态统计 + 占连接最多的对端。"""
    try:
        limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        limit = 15

    rc, out = _ss("-tanp")
    if rc != 0:
        return f"✗ 读连接失败（ss rc={rc}）：{out}"

    states: dict[str, int] = {}
    peers: dict[str, int] = {}
    detail = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        state, local, peer = parts[0], parts[3], parts[4]
        states[state] = states.get(state, 0) + 1
        if state == "ESTAB":
            host, port = _parse_ss_addr(peer)
            if host not in ("127.0.0.1", "::1"):
                key = f"{host}:{port}"
                peers[key] = peers.get(key, 0) + 1
        m = _SS_PROC_RE.search(" ".join(parts[5:]))
        if m and state in ("ESTAB", "SYN-SENT", "CLOSE-WAIT"):
            detail.append(f"  {state:<11} {local:<22} → {peer:<22} {m.group(1)}({m.group(2)})")

    lines = ["网络连接：", ""]
    lines.append("  状态分布  " + "  ".join(
        f"{k}={v}" for k, v in sorted(states.items(), key=lambda x: -x[1])
    ))
    if peers:
        lines.append("")
        lines.append("  对端 Top（非回环）")
        for k, v in sorted(peers.items(), key=lambda x: -x[1])[:limit]:
            lines.append(f"    {k:<40} ×{v}")
    if detail:
        lines.append("")
        lines.append("  明细（前 12 条）")
        lines.extend(detail[:12])
    if not states:
        lines.append("  （无连接）")
    return "\n".join(lines)


def net_ifaces() -> str:
    """网络接口：地址、状态、累计流量、错误包。"""
    rc, out = _run(["ip", "-br", "addr"])
    if rc != 0:
        return f"✗ 读接口失败（ip rc={rc}）：{out}"

    dev_stats = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, _, rest = line.partition(":")
                cols = rest.split()
                if len(cols) >= 16:
                    dev_stats[name.strip()] = {
                        "rx_bytes": int(cols[0]), "rx_errs": int(cols[2]),
                        "rx_drop": int(cols[3]),
                        "tx_bytes": int(cols[8]), "tx_errs": int(cols[10]),
                        "tx_drop": int(cols[11]),
                    }
    except Exception:  # noqa: BLE001
        pass

    lines = ["网络接口：", ""]
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        addrs = [p for p in parts[2:] if p not in ("UP", "DOWN")]
        s = dev_stats.get(name)
        traf = ""
        if s:
            traf = f"  收 {_human_kb(s['rx_bytes'] / 1024)} / 发 {_human_kb(s['tx_bytes'] / 1024)}"
            if s["rx_errs"] or s["tx_errs"]:
                traf += f"  ⚠ 错误包 收{s['rx_errs']}/发{s['tx_errs']}"
        lines.append(f"  {name:<8} {state:<6} {' '.join(addrs) if addrs else '(无地址)'}{traf}")

    # 单网卡是可用性风险 —— 但先查有没有云端兜底通道
    up = [l.split()[0] for l in out.splitlines()
          if len(l.split()) >= 2 and l.split()[1] == "UP"]
    # 用 pgrep 而不是 `systemctl is-active`：rpi-connect 是**用户级**服务，
    # 系统级查询会返回 inactive —— 直接漏判成「无兜底通道」，把可用性说低了。
    fallback = []
    for proc, label in (("rpi-connectd", "Raspberry Pi Connect"),
                        ("tailscaled", "Tailscale"),
                        ("zerotier-one", "ZeroTier")):
        rc2, out2 = _run(["pgrep", "-x", proc], timeout=5)
        if rc2 == 0 and out2.strip():
            fallback.append(label)
    if len(up) <= 1 and "wlan0" in up:
        lines.append("")
        lines.append("  ⚠ 只有无线网卡在线（eth0 未接网线）—— WiFi 一断就失去局域网直连")
        if fallback:
            lines.append(f"  ✓ 但有云端兜底通道在线：{'、'.join(fallback)}（可从外网接入，不是彻底失联）")
        else:
            lines.append("  ⚠ 未检测到云端兜底通道（tailscale/zerotier/rpi-connect）—— 远程操作前务必确认有物理接触")
    return "\n".join(lines)


def net_exposure() -> str:
    """暴露面审计：哪些服务对外可及，各自风险等级。"""
    rc, out = _ss("-tlnp")
    if rc != 0:
        return f"✗ 读监听失败（ss rc={rc}）：{out}"

    raw = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        host, port = _parse_ss_addr(parts[3])
        m = _SS_PROC_RE.search(" ".join(parts[5:]))
        raw.append({
            "port": port, "host": host,
            "name": m.group(1) if m else "?",
            "pid": m.group(2) if m else "?",
        })

    # 同样按端口合并双栈，否则同一个服务在 v4/v6 各报一次
    merged: dict[tuple[str, str], dict] = {}
    for r in raw:
        key = (r["port"], r["name"])
        if key in merged:
            merged[key]["hosts"].add(r["host"])
        else:
            merged[key] = {**r, "hosts": {r["host"]}}
    exposed, loopback = [], []
    for r in merged.values():
        if all(h in ("127.0.0.1", "::1", "localhost") for h in r["hosts"]):
            loopback.append(r)
        else:
            exposed.append(r)

    # 本机防火墙
    fw = []
    for tool, args in (("ufw", ["status"]), ("nft", ["list", "ruleset"])):
        if shutil.which(tool):
            r, o = _run([tool, *args], timeout=8)
            if r == 0:
                if tool == "ufw":
                    fw.append(f"ufw: {o.splitlines()[0] if o else '?'}")
                else:
                    n = o.count("rule") if o else 0
                    fw.append(f"nftables: 已配置（{n} 处 rule 关键字）")
    if not fw:
        fw.append("未检测到 ufw/nftables 规则 —— 本机无主机防火墙，全靠上游路由器")

    lines = [
        f"暴露面审计（对外监听 {len(exposed)} 个 / 本机回环 {len(loopback)} 个）",
        "",
        "  对外可及（同一局域网内任何人可尝试连接）：",
    ]
    if exposed:
        for it in sorted(exposed, key=lambda x: int(x["port"]) if x["port"].isdigit() else 0):
            stack = "v4+v6" if len(it["hosts"]) > 1 else (
                "v6" if any(":" in h for h in it["hosts"]) else "v4")
            lines.append(f"    :{it['port']:<6} {stack:<5} {it['name']}(pid {it['pid']})")
    else:
        lines.append("    （无 —— 全部只监听回环，很好）")

    lines.append("")
    lines.append("  仅本机回环（外部碰不到，安全）：")
    for it in sorted(loopback, key=lambda x: int(x["port"]) if x["port"].isdigit() else 0):
        lines.append(f"    :{it['port']:<6} {it['name']}(pid {it['pid']})")

    lines.append("")
    lines.append("  主机防火墙：")
    for f in fw:
        lines.append(f"    {f}")

    lines.append("")
    lines.append("  提示：cloudflared 隧道会把 :8000/:8001 暴露到公网域名，")
    lines.append("        这两个口上的服务是真正对互联网开放的，鉴权必须自己做好。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 存储支配

def storage_overview() -> str:
    """各挂载点使用率 + inode 使用率 + SD 卡磨损。"""
    rc, out = _run(["df", "-hP", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs"])
    if rc != 0:
        return f"✗ 读磁盘失败（df rc={rc}）：{out}"

    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, size, used, avail, pct, mount = parts[:6]
        try:
            p = int(pct.rstrip("%"))
        except ValueError:
            p = -1
        rows.append({"fs": fs, "size": size, "used": used, "avail": avail,
                     "pct": p, "mount": mount})

    lines = ["磁盘使用：", ""]
    for r in sorted(rows, key=lambda x: -x["pct"]):
        bar = "█" * min(20, max(0, r["pct"] // 5))
        warn = ""
        if r["pct"] >= 90:
            warn = "  ⚠ 危险，立刻清理"
        elif r["pct"] >= 80:
            warn = "  ⚠ 偏高"
        lines.append(
            f"  {r['mount']:<18} {r['used']:>6}/{r['size']:<6} {r['pct']:>3}%  {bar}{warn}"
        )

    # inode —— 小文件多的时候会先耗尽 inode，而 df -h 看着还有空间
    rc2, out2 = _run(["df", "-iP", "-x", "tmpfs", "-x", "devtmpfs"])
    if rc2 == 0:
        lines.append("")
        lines.append("  inode 使用（小文件过多会先耗这个）：")
        for line in out2.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                pct = int(parts[4].rstrip("%"))
            except ValueError:
                continue
            if pct > 20:
                lines.append(f"    {parts[5]:<18} {parts[2]:>9}/{parts[1]:<9} {pct}%")

    # SD 卡磨损。注意：节点不存在 ≠ 权限不足 —— 很多卡压根不上报这一项，
    # 写成「需 root」会骗用户白跑一趟 sudo（这次就真被误导过）。
    dev = "/sys/block/mmcblk0/device"
    life_path = f"{dev}/life_time"
    lines.append("")
    if os.path.exists(life_path):
        life = _read(life_path)
        vals = life.split()
        try:
            a = int(vals[0], 16)
            b = int(vals[1], 16) if len(vals) > 1 else 0
            if a == 0 and b == 0:
                lines.append("  SD 卡磨损  0x00 —— 未进入任何寿命损耗档位（很健康）")
            else:
                lines.append(f"  SD 卡磨损  0x{a:02x}/0x{b:02x} —— 已进入损耗档位，注意备份")
        except (ValueError, IndexError):
            lines.append(f"  SD 卡磨损  原始值 {life!r}")
    else:
        name = _read(f"{dev}/name") or "?"
        date = _read(f"{dev}/date") or "?"
        lines.append("  SD 卡磨损  本卡不上报（life_time 节点不存在，**不是权限问题**）")
        lines.append(f"             型号 {name}，出厂 {date}")
        stat = _read("/sys/block/mmcblk0/stat")
        f = stat.split() if stat else []
        if len(f) >= 7:
            try:
                rd_n, rd_sec = int(f[0]), int(f[2])
                wr_n, wr_sec = int(f[4]), int(f[6])
                lines.append(
                    f"             累计 读 {rd_n:,} 次（{rd_sec * 512 / 2**30:.1f}GB）"
                    f" / 写 {wr_n:,} 次（{wr_sec * 512 / 2**30:.1f}GB）"
                )
            except (ValueError, IndexError):
                pass
        lines.append("             ⚠ 树莓派最脆弱的一环就是 SD 卡 —— 定期备份，别等它坏")
    return "\n".join(lines)


def storage_usage(path: str = "/home/wxf", depth: int = 1, limit: int = 12) -> str:
    """目录占用排行。"""
    path = (path or "/home/wxf").strip()
    if not os.path.isdir(path):
        return f"✗ 目录不存在：{path}"
    try:
        depth = max(0, min(int(depth), 4))
        limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        depth, limit = 1, 12

    rc, out = _run(["du", f"-xhd{depth}", path], timeout=60)
    if rc != 0:
        return f"✗ du 失败（rc={rc}）：{out}"

    entries = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        size, p = parts
        entries.append((_parse_du_kb(size), p))
    if not entries:
        return f"（{path} 下无内容或全部无权限）"

    entries.sort(reverse=True)
    lines = [f"目录占用：{path}（深度 {depth}，Top {limit}）", ""]
    for kb, p in entries[:limit]:
        lines.append(f"  {_human_kb(kb):>9}  {p}")
    return "\n".join(lines)


def _parse_du_kb(size: str) -> float:
    """du 的 `1.2G` / `892M` / `512K` → KB 数。"""
    size = size.strip()
    mult = {"K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}
    if size and size[-1] in mult:
        try:
            return float(size[:-1]) * mult[size[-1]]
        except ValueError:
            return 0.0
    try:
        return float(size)
    except ValueError:
        return 0.0


def storage_bigfiles(path: str = "/home/wxf", min_mb: int = 50, limit: int = 15) -> str:
    """找出大文件 —— 空间到底被什么吃了。"""
    path = (path or "/home/wxf").strip()
    if not os.path.isdir(path):
        return f"✗ 目录不存在：{path}"
    try:
        min_mb = max(1, min(int(min_mb), 10000))
        limit = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        min_mb, limit = 50, 15

    rc, out = _run([
        "find", path, "-xdev", "-type", "f",
        "-size", f"+{min_mb}M", "-printf", "%s\t%TY-%Tm-%Td\t%p\n",
    ], timeout=90)
    if rc != 0:
        return f"✗ find 失败（rc={rc}）：{out}"
    if not out.strip():
        return f"（{path} 下没有大于 {min_mb}MB 的文件）"

    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), parts[1], parts[2]))
        except ValueError:
            continue
    rows.sort(reverse=True)

    lines = [f"大文件（>{min_mb}MB，共 {len(rows)} 个）：", ""]
    for size, mtime, p in rows[:limit]:
        lines.append(f"  {_human_kb(size / 1024):>9}  {mtime}  {p}")
    return "\n".join(lines)


def storage_clean_candidates() -> str:
    """缓存清理候选 —— 只统计，不删。删不删由人决定。"""
    targets = [
        ("~/.cache/pip", "pip 下载缓存，删了下次装包重新下"),
        ("~/.cache/huggingface", "模型下载缓存，删了要重新下模型（大）"),
        ("~/.cache/node-gyp", "node-gyp 编译缓存"),
        ("~/.cache/pnpm", "pnpm store"),
        ("~/.cache/chromium", "无头浏览器缓存"),
        ("~/.npm/_cacache", "npm 包缓存，删了下次 npm i 重新下"),
        ("~/.cache/ms-playwright", "Playwright 浏览器内核，删了要重装"),
        ("/var/cache/apt/archives", "apt 已下载的 deb 包，删了完全无害"),
        ("/var/log", "系统日志（journald 是 volatile，这里主要是旧文件）"),
    ]

    lines = ["清理候选（只统计，未删除）：", ""]
    total = 0.0
    for raw, note in targets:
        p = os.path.expanduser(raw)
        if not os.path.isdir(p):
            continue
        rc, out = _run(["du", "-sh", p], timeout=60)
        if rc != 0:
            continue
        size = out.split("\t", 1)[0].strip()
        kb = _parse_du_kb(size)
        total += kb
        lines.append(f"  {size:>8}  {raw:<26} {note}")

    if len(lines) == 2:
        lines.append("  （没找到可清理的缓存目录）")
    else:
        lines.append("")
        lines.append(f"  合计约 {_human_kb(total)}")
        lines.append("  ⚠ 安全删法：pip/npm/apt 缓存可放心删；huggingface 与 playwright")
        lines.append("    删了要重新下载（树莓派上很慢），非必要别动。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 安全审计

# 审计项的严重级别 → 展示顺序
_SEV_ORDER = {"HIGH": 0, "MED": 1, "INFO": 2, "OK": 3}
_SEV_ICON = {"HIGH": "🔴", "MED": "🟠", "INFO": "🔵", "OK": "🟢"}


def _audit_exposure() -> list[tuple[str, str, str]]:
    """对外监听的端口 —— 每多一个就是一个攻击面。"""
    rc, out = _ss("-tlnp")
    if rc != 0:
        return [("INFO", "暴露面", f"读不到监听端口（ss rc={rc}）")]
    exposed = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        host, port = _parse_ss_addr(parts[3])
        if host not in ("127.0.0.1", "::1", "localhost"):
            exposed.add(port)

    findings = []
    if "111" in exposed:
        findings.append((
            "MED", "rpcbind 对外暴露（:111）",
            "树莓派上 rpcbind 通常只被 NFS 用到，没跑 NFS 就是纯多余的攻击面。"
            "关掉：sudo systemctl disable --now rpcbind rpcbind.socket",
        ))
    if "23" in exposed:
        findings.append(("HIGH", "telnet 开放（:23）", "明文协议，凭据会被抓包。立刻关掉。"))
    if "445" in exposed:
        findings.append(("HIGH", "SMB 开放（:445）", "勒索软件的主要入口。除非确实要共享文件，否则关掉。"))
    for p in ("3306", "6379", "27017", "5432"):
        if p in exposed:
            findings.append((
                "HIGH", f"数据库端口对外（:{p}）",
                "数据库不该直接暴露在网络上。改成只听 127.0.0.1，或走 SSH 隧道。",
            ))
    if not findings:
        findings.append((
            "OK", "对外监听",
            f"共 {len(exposed)} 个对外端口：{', '.join(sorted(exposed, key=int))}",
        ))
    return findings


def _audit_ssh() -> list[tuple[str, str, str]]:
    """SSH 是唯一的常规远程入口，它的配置最值得看。"""
    cfg = _read("/etc/ssh/sshd_config")
    if not cfg:
        return [("INFO", "SSH 配置", "读不到 /etc/ssh/sshd_config（可能需要 root）")]

    findings = []
    eff: dict[str, str] = {}
    for line in cfg.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            eff[parts[0].lower()] = parts[1].strip()

    # sshd_config 里 Include 的片段才常放这些项，主文件可能没有 —— 查不到就说查不到
    pw = eff.get("passwordauthentication")
    if pw and pw.lower() == "yes":
        findings.append((
            "MED", "SSH 允许密码登录",
            "公网可达的 SSH 开着密码认证，等于允许无限次暴力猜。"
            "改用密钥：PasswordAuthentication no（先确认密钥能登，别把自己锁外面）",
        ))
    root = eff.get("permitrootlogin")
    if root and root.lower() in ("yes", "without-password", "prohibit-password"):
        findings.append((
            "MED", f"SSH 允许 root 登录（{root}）",
            "建议 PermitRootLogin no，用普通账号 + sudo。",
        ))
    if not findings:
        findings.append((
            "OK", "SSH 主配置",
            "未发现明显的弱配置（注意：实际生效值还受 /etc/ssh/sshd_config.d/ 影响）",
        ))
    return findings


def _audit_accounts() -> list[tuple[str, str, str]]:
    """账号与提权面。"""
    findings = []
    rc, out = _run(["getent", "group", "sudo"])
    if rc == 0 and out:
        members = out.split(":")[-1].split(",")
        members = [m for m in members if m]
        if members:
            findings.append((
                "INFO", "sudo 组成员",
                f"{', '.join(members)} —— 这几个账号都能拿到 root，请确认都是必要的",
            ))

    # 可登录账号 —— 必须拿 /etc/shells 白名单过滤。
    # 只排 nologin/false 不够：sync 的 shell 是 /bin/sync，而 /etc/passwd 里
    # 还有一大票系统账号，用黑名单会列出三十多个假阳性，反而淹没真信号。
    try:
        with open("/etc/shells") as f:
            valid_shells = {l.strip() for l in f
                            if l.strip() and not l.startswith("#")}
    except Exception:  # noqa: BLE001
        valid_shells = {"/bin/sh", "/bin/bash", "/bin/dash", "/usr/bin/bash"}

    loginable = []
    parse_error = ""
    try:
        with open("/etc/passwd") as f:
            for line in f:
                # 必须 strip —— 直接 split(":") 时最后一个字段会带着 "\n"，
                # 结果 shell 永远匹配不上 /etc/shells，静默返回空列表。
                # 这类「不报错但结果是错的」比抛异常危险得多。
                parts = line.strip().split(":")
                if len(parts) >= 7 and parts[6] in valid_shells:
                    loginable.append(f"{parts[0]}(uid {parts[2]})")
    except Exception as e:  # noqa: BLE001
        parse_error = f"{type(e).__name__}: {e}"

    if loginable:
        findings.append((
            "INFO", "可交互登录账号",
            f"{', '.join(loginable)}\n    按 /etc/shells 判定 —— 只有这些账号能真的登进来",
        ))
    else:
        findings.append((
            "MED", "可交互登录账号解析异常",
            f"解析结果为空{'（' + parse_error + '）' if parse_error else ''} —— "
            f"正常情况下至少有 root。这说明解析逻辑出了问题，而不是「真的没账号」。",
        ))

    # 空密码账号需要读 shadow
    shadow = _read("/etc/shadow")
    if shadow:
        empty = []
        for line in shadow.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "":
                empty.append(parts[0])
        if empty:
            findings.append((
                "HIGH", "存在空密码账号",
                f"{', '.join(empty)} —— 任何人都能直接登录。立刻设密码或用 passwd -l 锁定。",
            ))
        else:
            findings.append(("OK", "空密码账号", "无（所有账号都有密码哈希）"))
    else:
        findings.append(("INFO", "空密码检查", "读不到 /etc/shadow，跳过（需 root）"))
    return findings


def _audit_suid() -> list[tuple[str, str, str]]:
    """SUID 文件 —— 提权漏洞的高发地。"""
    dirs = [d for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin",
                        "/usr/local/bin", "/usr/local/sbin") if os.path.isdir(d)]
    if not dirs:
        return [("INFO", "SUID 扫描", "标准 bin 目录都不存在，跳过")]

    rc, out = _run([
        "find", *dirs, "-xdev", "-type", "f", "-perm", "-4000",
        "-printf", "%p\n",
    ], timeout=60)
    if rc != 0:
        return [("INFO", "SUID 扫描", f"find 失败（rc={rc}）：{out}")]

    files = [f for f in out.splitlines() if f.strip()]
    # 常见合法 SUID，不算异常
    known = {"sudo", "su", "passwd", "chsh", "chfn", "newgrp", "gpasswd",
             "mount", "umount", "pkexec", "fusermount", "fusermount3",
             "ping", "ssh-agent", "ntfs-3g", "polkit-agent-helper-1",
             "dbus-daemon-launch-helper", "unix_chkpwd", "chage",
             # 远程桌面 / 网络挂载类，装了对应组件就一定会出现
             "pppd", "mount.cifs", "mount.nfs", "vncserver-x11", "Xvnc",
             "vmware-user-suid-wrapper", "chrome-sandbox", "sudo.ws",
             "exim4", "procmail", "at"}
    odd = [f for f in files if os.path.basename(f) not in known]

    findings = [("INFO", "SUID 文件",
                 f"共 {len(files)} 个（都是 root 拥有的提权入口）")]
    if odd:
        findings.append((
            "MED", "非典型的 SUID 文件",
            f"{', '.join(odd[:8])}{' …' if len(odd) > 8 else ''}\n"
            f"    这些不是系统自带的常见项。确认是不是你自己装的工具；"
            f"不认识的 SUID 是后门最常见的形式。",
        ))
    else:
        findings.append(("OK", "SUID 异常项", "无 —— 全部是系统自带的常见 SUID"))
    return findings


def _audit_processes() -> list[tuple[str, str, str]]:
    """可疑进程：从临时目录运行、可执行文件已被删除。"""
    suspicious, deleted = [], []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = entry
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe.endswith(" (deleted)"):
            deleted.append((pid, exe.replace(" (deleted)", "")))
        elif exe.startswith(("/tmp/", "/var/tmp/", "/dev/shm/")):
            suspicious.append((pid, exe))

    findings = []
    if suspicious:
        findings.append((
            "HIGH", "从临时目录运行的进程",
            "\n".join(f"    pid {p} → {e}" for p, e in suspicious[:6]) +
            "\n    正常服务不会把可执行文件放 /tmp 或 /dev/shm —— 这是恶意程序的典型行为。",
        ))
    if deleted:
        findings.append((
            "MED", "可执行文件已被删除的进程",
            "\n".join(f"    pid {p} → {e}" for p, e in deleted[:6]) +
            "\n    可能是升级后没重启，也可能是攻击者删掉痕迹。",
        ))
    if not findings:
        findings.append(("OK", "进程检查", "无可疑进程（无临时目录运行 / 无已删除可执行）"))
    return findings


def _audit_services() -> list[tuple[str, str, str]]:
    """失败的服务 —— 挂了没人知道是最常见的问题。"""
    rc, out = _run(["systemctl", "list-units", "--state=failed", "--no-legend",
                    "--no-pager"], timeout=15)
    failed = [l.split()[0] for l in out.splitlines() if l.strip()] if rc == 0 else []
    findings = []
    if failed:
        findings.append((
            "MED", "失败的服务",
            f"{', '.join(failed)}\n    用 linux_service(action='logs', unit=...) 看原因。",
        ))
    else:
        findings.append(("OK", "服务状态", "系统级服务无失败项"))

    # 自动安全更新
    rc2, out2 = _run(["systemctl", "is-enabled", "unattended-upgrades"], timeout=8)
    if rc2 == 0:
        state = out2.strip()
        findings.append((
            "OK" if state == "enabled" else "MED",
            "自动安全更新",
            f"unattended-upgrades = {state}" + (
                "" if state == "enabled"
                else " —— 树莓派常期在线，建议开启：sudo dpkg-reconfigure unattended-upgrades"
            ),
        ))
    return findings


def audit() -> str:
    """系统安全审计：暴露面 / SSH / 账号 / SUID / 进程 / 服务。只读，不改任何配置。"""
    sections = [
        ("暴露面", _audit_exposure),
        ("SSH", _audit_ssh),
        ("账号与提权", _audit_accounts),
        ("SUID 提权面", _audit_suid),
        ("可疑进程", _audit_processes),
        ("服务与更新", _audit_services),
    ]

    all_findings: list[tuple[str, str, str]] = []
    for _name, fn in sections:
        try:
            all_findings.extend(fn())
        except Exception as e:  # noqa: BLE001
            all_findings.append(("INFO", f"{_name} 检查异常", f"{type(e).__name__}: {e}"))

    counts = {}
    for sev, _, _ in all_findings:
        counts[sev] = counts.get(sev, 0) + 1

    if counts.get("HIGH"):
        verdict = "需要处理（有高危项）"
    elif counts.get("MED"):
        verdict = "基本可用（有中危项值得看）"
    else:
        verdict = "未见明显问题"

    lines = [
        f"安全审计：{verdict}",
        f"  高危 {counts.get('HIGH', 0)}  中危 {counts.get('MED', 0)}  "
        f"提示 {counts.get('INFO', 0)}  正常 {counts.get('OK', 0)}",
        "",
    ]
    for sev in sorted({f[0] for f in all_findings}, key=lambda s: _SEV_ORDER[s]):
        for s, title, detail in all_findings:
            if s != sev:
                continue
            lines.append(f"{_SEV_ICON[s]} [{s}] {title}")
            for dl in detail.splitlines():
                lines.append(f"    {dl}" if dl.strip() else "")
            lines.append("")
    lines.append("  说明：全部检查只读，未修改任何系统配置。")
    return "\n".join(lines)
