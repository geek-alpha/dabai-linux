"""Linux 感官 —— 让大白「感觉」到自己跑在一台真实的机器上。

第一性原理：Linux 把硬件状态全部暴露为**文件**（/sys、/proc），
读取即感知，零特权、零依赖、零轮询成本。所以「与系统一体」不是比喻，是数据通路。

数据源（全部只读、无需 root）：
    /sys/class/thermal/thermal_zone0/temp   SoC 温度
    vcgencmd get_throttled                  欠压 / 降频（树莓派独有，最能救命）
    /sys/block/zram0/mm_stat                zram 压缩比（内存不够时的真实余量）
    /proc/meminfo, /proc/loadavg, /proc/pressure/*, /proc/self/status

设计原则：
1. **绝不抛异常**——任何一项读不到就标记 unavailable，其余照常给（部分感知 > 全盘失败）；
2. **只读**——本模块不改任何系统状态，感知与行动分离；
3. **可判定**——不止返回数字，还给 verdict 与 advice，让模型能据此决策。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------- 基础读取

def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace").strip()
    except Exception:
        return None


def _read_int(path: str) -> int | None:
    v = _read(path)
    if v is None:
        return None
    try:
        return int(v.split()[0])
    except (ValueError, IndexError):
        return None


def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    """跑一条只读命令；失败/超时返回 None（不抛）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------- SoC / 温度

# 树莓派 get_throttled 位定义：bit0-3 = 当前状态，bit16-19 = 历史曾发生
_THROTTLE_BITS = (
    (0x00001, "欠压", "now", "critical"),
    (0x00002, "ARM 频率被限制", "now", "warn"),
    (0x00004, "正在降频", "now", "critical"),
    (0x00008, "软温度限制生效", "now", "critical"),
    (0x10000, "欠压", "past", "warn"),
    (0x20000, "频率限制", "past", "info"),
    (0x40000, "降频", "past", "warn"),
    (0x80000, "软温度限制", "past", "warn"),
)

# 树莓派阈值：软限制 80°C 起降频，硬限制 85°C 强制降频
TEMP_WARM = 60.0
TEMP_HOT = 72.0
TEMP_CRITICAL = 80.0


def soc() -> dict:
    """SoC 身份与热状态：型号 / 温度 / 频率 / 电压 / 欠压降频。"""
    out: dict = {"available": False}

    model = _read("/proc/device-tree/model")
    if model:
        out["model"] = model.replace("\x00", "").strip()

    milli = _read_int("/sys/class/thermal/thermal_zone0/temp")
    if milli is not None:
        out["temp_c"] = round(milli / 1000.0, 1)

    # 温度分级
    t = out.get("temp_c")
    if t is not None:
        if t >= TEMP_CRITICAL:
            out["thermal_state"] = "critical"
        elif t >= TEMP_HOT:
            out["thermal_state"] = "hot"
        elif t >= TEMP_WARM:
            out["thermal_state"] = "warm"
        else:
            out["thermal_state"] = "cool"

    gov = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov:
        out["governor"] = gov

    khz = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if khz:
        out["freq_mhz"] = round(khz / 1000)

    # vcgencmd：树莓派固件接口，无需 root，是最可靠的硬件遥测来源
    v = _run(["vcgencmd", "get_throttled"])
    if v and "=" in v:
        try:
            raw = int(v.split("=", 1)[1], 16)
            flags = []
            for bit, label, when, level in _THROTTLE_BITS:
                if raw & bit:
                    flags.append({"what": label, "when": when, "level": level})
            out["throttled_raw"] = f"0x{raw:x}"
            out["throttled"] = flags
            out["power_ok"] = raw == 0
        except ValueError:
            pass

    volt = _run(["vcgencmd", "measure_volts", "core"])
    if volt and "=" in volt:
        out["core_volt"] = volt.split("=", 1)[1]

    if out.get("model") or t is not None:
        out["available"] = True
    return out


# ---------------------------------------------------------------- 内存

def memory() -> dict:
    """RAM / Swap / zram —— 小内存机器上最关键的指标。"""
    out: dict = {"available": False}
    info: dict[str, int] = {}
    raw = _read("/proc/meminfo")
    if raw:
        for line in raw.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            parts = v.split()
            if parts:
                try:
                    info[k.strip()] = int(parts[0])
                except ValueError:
                    pass

    def mb(key: str) -> float | None:
        v = info.get(key)
        return round(v / 1024.0, 1) if v is not None else None

    out["total_mb"] = mb("MemTotal")
    out["available_mb"] = mb("MemAvailable")
    out["free_mb"] = mb("MemFree")
    out["cached_mb"] = mb("Cached")
    out["swap_total_mb"] = mb("SwapTotal")
    out["swap_free_mb"] = mb("SwapFree")
    if out["swap_total_mb"]:
        used = out["swap_total_mb"] - (out["swap_free_mb"] or 0)
        out["swap_used_mb"] = round(used, 1)
        out["swap_used_pct"] = round(used / out["swap_total_mb"] * 100, 1)
    if out["total_mb"] and out["available_mb"] is not None:
        out["used_pct"] = round((out["total_mb"] - out["available_mb"]) / out["total_mb"] * 100, 1)

    # zram：压缩内存当 swap 用。⚠ 实测 /sys/block/zram0/ 下**没有** orig_data_size /
    # compr_data_size 这两个节点（写过、永远是 None），真实数据源是 mm_stat，空格分隔：
    #   orig_data_size compr_data_size mem_used_total mem_limit mem_used_max same_pages ...
    z_disk = _read_int("/sys/block/zram0/disksize")
    if z_disk:
        out["zram_size_mb"] = round(z_disk / 1024 / 1024, 1)
        out["zram"] = True
        mm = _read("/sys/block/zram0/mm_stat")
        if mm:
            parts = mm.split()
            try:
                z_orig, z_compr = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                z_orig = z_compr = 0
            if z_orig > 0 and z_compr > 0:
                out["zram_stored_mb"] = round(z_orig / 1024 / 1024, 1)
                out["zram_compressed_mb"] = round(z_compr / 1024 / 1024, 1)
                out["zram_ratio"] = round(z_orig / z_compr, 2)

    if out["total_mb"]:
        out["available"] = True
    return out


# ---------------------------------------------------------------- CPU / 负载

def cpu() -> dict:
    out: dict = {"cores": os.cpu_count()}
    la = _read("/proc/loadavg")
    if la:
        parts = la.split()
        try:
            out["load1"] = float(parts[0])
            out["load5"] = float(parts[1])
            out["load15"] = float(parts[2])
            if out.get("cores"):
                out["load1_pct"] = round(out["load1"] / out["cores"] * 100, 1)
        except (ValueError, IndexError):
            pass
    return out


# ---------------------------------------------------------------- 压力感知（PSI）

def pressure() -> dict:
    """PSI（/proc/pressure/*）——内核给出的真实资源阻塞时长。

    比 load average 准得多：load 把「等 IO」和「等 CPU」混在一起，
    PSI 直接告诉你「有多少时间在因为内存不够而卡住」。
    树莓派内核可能未编译 PSI，此时明确标 unavailable。
    """
    out: dict = {"available": False, "note": "内核未启用 PSI"}
    got = False
    for res in ("cpu", "memory", "io"):
        raw = _read(f"/proc/pressure/{res}")
        if not raw:
            continue
        got = True
        entry: dict = {}
        for line in raw.splitlines():
            parts = line.split()
            if not parts:
                continue
            kind = parts[0]
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    entry[f"{kind}_{k}"] = float(v.rstrip("%"))
        if entry:
            out[res] = entry
    if got:
        out["available"] = True
        out.pop("note", None)
    return out


# ---------------------------------------------------------------- 存储

def storage(path: str = "/") -> dict:
    out: dict = {"path": path, "available": False}
    try:
        total, used, free = shutil.disk_usage(path)
        out["total_gb"] = round(total / 1024 ** 3, 1)
        out["used_gb"] = round(used / 1024 ** 3, 1)
        out["free_gb"] = round(free / 1024 ** 3, 1)
        out["used_pct"] = round(used / total * 100, 1)
        out["available"] = True
    except Exception:
        pass
    # SD 卡写入寿命：根分区挂载参数里的 noatime/ro 很重要
    mounts = _read("/proc/mounts") or ""
    for line in mounts.splitlines():
        f = line.split()
        if len(f) >= 4 and f[1] == path:
            out["fs"] = f[2]
            out["mount_opts"] = f[3]
            out["noatime"] = "noatime" in f[3]
            out["device"] = f[0]
            break
    return out


# ---------------------------------------------------------------- 自身开销

def self_usage(pid: int | None = None) -> dict:
    """大白自己吃了多少——进程级真相（/proc/self）。"""
    pid = pid or os.getpid()
    out: dict = {"pid": pid, "available": False}
    status = _read(f"/proc/{pid}/status")
    if status:
        for line in status.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            if k in ("VmRSS", "VmSize", "VmSwap"):
                try:
                    out[k.lower()] = round(int(v.split()[0]) / 1024.0, 1)
                except (ValueError, IndexError):
                    pass
            elif k == "Threads":
                try:
                    out["threads"] = int(v.strip())
                except ValueError:
                    pass
        out["available"] = "vmrss" in out
    try:
        out["open_fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 汇总判定

def senses(full: bool = True) -> dict:
    """一次拿到全部感官数据 + 判定 + 建议。"""
    data: dict = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": _read("/proc/sys/kernel/hostname") or "",
        "soc": soc(),
        "memory": memory(),
        "cpu": cpu(),
        "storage": storage(),
        "self": self_usage(),
    }
    if full:
        data["pressure"] = pressure()

    data["verdict"], data["advice"] = _judge(data)
    return data


def _judge(d: dict) -> tuple[str, list[str]]:
    """把数字翻译成判定与建议——这是「感知」和「看见」的区别。"""
    advice: list[str] = []
    level = 0  # 0 舒适 / 1 注意 / 2 吃紧 / 3 危险

    s, m, c, st = d["soc"], d["memory"], d["cpu"], d["storage"]

    # 感知失败必须报「未知」，不能报「健康」——把读不到当正常是最危险的假阳性
    if not (s.get("available") or m.get("available") or st.get("available")):
        return "unknown", ["所有硬件数据源均不可读（非 Linux 或权限受限），无法判断系统状态"]

    # 温度
    ts = s.get("thermal_state")
    if ts == "critical":
        level = max(level, 3)
        advice.append(f"SoC {s.get('temp_c')}°C 已到降频线，重活先别派，或加散热")
    elif ts == "hot":
        level = max(level, 2)
        advice.append(f"SoC {s.get('temp_c')}°C 偏高，避免并行跑多个重任务")
    elif ts == "warm":
        level = max(level, 1)
        advice.append(
            f"SoC {s.get('temp_c')}°C 偏暖（距降频线 {TEMP_CRITICAL - (s.get('temp_c') or 0):.0f}°C），"
            "长任务注意散热"
        )

    # 欠压 / 降频（树莓派最容易被忽略的坑：电源不够，性能悄悄掉一半）
    if s.get("throttled"):
        now_flags = [f["what"] for f in s["throttled"] if f["when"] == "now"]
        past_flags = [f["what"] for f in s["throttled"] if f["when"] == "past"]
        if now_flags:
            level = max(level, 3)
            advice.append(f"正在 {', '.join(now_flags)} —— 立刻换 5V/2.5A 以上电源")
        elif past_flags:
            level = max(level, 1)
            advice.append(f"历史上出现过 {', '.join(past_flags)}，电源余量不足")

    # 内存（小内存机器的主要死因）
    avail = m.get("available_mb")
    if avail is not None:
        if avail < 60:
            level = max(level, 3)
            advice.append(f"可用内存仅 {avail}MB，随时可能 OOM，先清进程")
        elif avail < 120:
            level = max(level, 2)
            advice.append(f"可用内存 {avail}MB 偏紧，重活分批做")
        elif avail < 200:
            level = max(level, 1)
    if (m.get("swap_used_pct") or 0) > 60:
        level = max(level, 2)
        advice.append(
            f"Swap 已用 {m.get('swap_used_pct')}%（zram 压缩比 {m.get('zram_ratio', '?')}:1）"
            "——内存压力真实存在，不是缓存假象"
        )

    # 负载
    if c.get("load1_pct") is not None and c["load1_pct"] > 150:
        level = max(level, 2)
        advice.append(f"1 分钟负载 {c['load1']} 已超核数 {c.get('cores')} 的 1.5 倍")
    elif c.get("load1_pct") is not None and c["load1_pct"] > 100:
        level = max(level, 1)

    # 磁盘
    if st.get("free_gb") is not None and st["free_gb"] < 1.0:
        level = max(level, 2)
        advice.append(f"磁盘只剩 {st['free_gb']}GB，先清理再下大文件")

    verdict = ("healthy", "notice", "strained", "critical")[level]
    if not advice:
        advice.append("各项正常，可以放心跑重活")
    return verdict, advice


def thermal_guard(need: str = "normal") -> tuple[bool, str]:
    """派重活前的安全检查。

    need: light（闲聊/搜索）/ normal（改码/任务）/ heavy（编译/模型/批量）
    返回 (是否放行, 理由)。这是把「感知」变成「决策」的那一步。
    """
    s = soc()
    m = memory()
    c = cpu()

    if not (s.get("available") or m.get("available")):
        return True, "无法感知系统状态（数据源不可读），按放行处理"

    temp = s.get("temp_c")
    avail = m.get("available_mb")
    load = c.get("load1") or 0.0
    cores = c.get("cores") or 1

    if need == "heavy":
        if temp is not None and temp >= TEMP_HOT:
            return False, f"SoC {temp}°C 过热，重活会触发降频（得不偿失）"
        if avail is not None and avail < 150:
            return False, f"可用内存仅 {avail}MB，重活可能触发 OOM"
        if load > cores * 1.5:
            return False, f"负载 {load} 已饱和，排队更划算"

    if need in ("normal", "heavy"):
        if avail is not None and avail < 60:
            return False, f"可用内存 {avail}MB 已到危险线"
        if s.get("thermal_state") == "critical":
            return False, f"SoC {temp}°C 正在降频"

    return True, "资源充足"


# ---------------------------------------------------------------- 人类可读

_STATE_ICON = {"healthy": "✓", "notice": "·", "strained": "!", "critical": "✗", "unknown": "?"}
_THERMAL_ICON = {"cool": "❄", "warm": "·", "hot": "▲", "critical": "✗"}


def format_report(d: dict | None = None) -> str:
    d = d or senses()
    s, m, c, st, me = d["soc"], d["memory"], d["cpu"], d["storage"], d["self"]

    lines = [
        f"{_STATE_ICON.get(d['verdict'], '·')} 系统状态：{d['verdict']}   ({d['ts']})",
    ]
    if s.get("model"):
        lines.append(f"  硬件   {s['model']}")
    if s.get("temp_c") is not None:
        lines.append(
            f"  温度   {s['temp_c']}°C {_THERMAL_ICON.get(s.get('thermal_state'), '')}"
            f"   频率 {s.get('freq_mhz', '?')}MHz   调频 {s.get('governor', '?')}"
        )
    if s.get("throttled_raw"):
        ok = s.get("power_ok")
        lines.append(f"  电源   {'正常' if ok else '异常'} (throttled={s['throttled_raw']})")
    if m.get("total_mb"):
        zr = f"   zram {m.get('zram_ratio', '?')}:1" if m.get("zram_ratio") else ""
        lines.append(
            f"  内存   {m.get('available_mb')}MB 可用 / {m['total_mb']}MB 总"
            f"   Swap {m.get('swap_used_mb', 0)}/{m.get('swap_total_mb', 0)}MB{zr}"
        )
    if c.get("load1") is not None:
        lines.append(f"  负载   {c['load1']} / {c['load5']} / {c['load15']}   ({c.get('cores')} 核)")
    if st.get("free_gb") is not None:
        lines.append(
            f"  磁盘   {st['free_gb']}GB 可用 / {st['total_gb']}GB"
            f"   ({st.get('fs', '?')}{', noatime' if st.get('noatime') else ''})"
        )
    if me.get("vmrss"):
        lines.append(
            f"  自身   {me['vmrss']}MB RSS   {me.get('threads', '?')} 线程"
            f"   {me.get('open_fds', '?')} 个 fd"
        )
    p = d.get("pressure") or {}
    if p.get("available"):
        bits = []
        for res in ("cpu", "memory", "io"):
            if res in p and "some_avg10" in p[res]:
                bits.append(f"{res} {p[res]['some_avg10']}%")
        if bits:
            lines.append(f"  阻塞   {'   '.join(bits)}  (PSI avg10，内核真实阻塞时长)")

    lines.append("  建议   " + "；".join(d["advice"]))
    return "\n".join(lines)
