#!/usr/bin/env python3
"""GPIO 硬件支配 —— 大白伸进物理世界的入口。

树莓派 40-pin 排针：读引脚功能/电平、驱动输出、闪灯。
板载 ACT/PWR LED 可作为物理状态指示（需 root）。

为什么用 pinctrl 而不是 gpiozero/RPi.GPIO：
    本机三个 Python 库都装了（gpiozero / RPi.GPIO / lgpio），但版本行为未知、
    且 pin_factory 延迟初始化的失败模式不透明。pinctrl 是树莓派官方调试工具，
    芯片映射正确、输出可直接解析、无库版本兼容风险。读用 `pinctrl get`，
    写用 `pinctrl set`，一条命令一个事实。

安全边界（硬规则，不可绕过）：
    1. 写操作只允许「通用 IO」白名单引脚。HAT EEPROM / SPI / UART / I2C 专用脚
       一律拒绝 —— 尤其 GPIO0/1（ID_EEPROM），误写会让系统认错扩展板。
    2. 写之前确认引脚当前不是 alt function（别的驱动正占着，抢过来会搞坏对方）。
    3. 默认只读。写/闪灯必须显式指定 action，不存在「顺手写一下」。
    4. 引脚回到 input 是收尾动作，不留在 output 悬空（避免接错线烧外设）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

_PINCTRL = shutil.which("pinctrl") or "/usr/bin/pinctrl"

# 可以安全读写的「通用 IO」引脚（BCM 编号）
GENERAL_PINS = (4, 5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27)

# 专用功能引脚 —— 只读不写
RESERVED_PINS = {
    0: "ID_SDA / HAT EEPROM 数据线",
    1: "ID_SCL / HAT EEPROM 时钟线",
    2: "SDA1 / I2C 数据",
    3: "SCL1 / I2C 时钟",
    7: "SPI0_CE1 / SPI 片选",
    8: "SPI0_CE0 / SPI 片选",
    9: "SPI0_MISO / SPI 数据入",
    10: "SPI0_MOSI / SPI 数据出",
    11: "SPI0_SCLK / SPI 时钟",
    14: "TXD / 串口发送（默认控制台）",
    15: "RXD / 串口接收",
    28: "GPIO28 / 板载扩展（通常不可用）",
    29: "LAN_RUN_BOOT / 板载网卡控制",
    40: "PWM0_OUT / 音频左声道",
    41: "PWM1_OUT / 音频右声道",
    42: "ETH_CLK / 网卡时钟",
    43: "WIFI_CLK / WiFi 时钟",
    44: "SDA0 / 保留 I2C",
    45: "SCL0 / 保留 I2C",
    46: "SMPS_SCL / 电源管理 I2C 时钟（写坏会影响供电）",
    47: "SMPS_SDA / 电源管理 I2C 数据（写坏会影响供电）",
    48: "SD_CLK / SD 卡控制器（写坏可能损坏卡上数据）",
    49: "SD_CMD / SD 卡控制器",
    50: "SD_DATA0 / SD 卡控制器",
    51: "SD_DATA1 / SD 卡控制器",
    52: "SD_DATA2 / SD 卡控制器",
    53: "SD_DATA3 / SD 卡控制器",
}

_MAX_BCM = 53  # 物理 GPIO 上限。pinctrl 还会列出 100+ 的 FWGPIO（固件内部信号，不对应排针）

_FUNC_LABEL = {
    "ip": "输入",
    "op": "输出",
    "no": "未用",
    "a0": "alt0", "a1": "alt1", "a2": "alt2",
    "a3": "alt3", "a4": "alt4", "a5": "alt5",
}

# 两种格式都要吃下：
#   `17: ip    -- | lo // GPIO17 = input`      （输入脚：功能 + 拉电阻）
#   `47: op -- -- | hi // SMPS_SDA/GPIO47`     （输出脚：多一列，pinctrl 自己格式不一致）
_LINE_RE = re.compile(r"^\s*(\d+):\s+(.+?)\s*\|\s*(\S+)\s*//\s*(.+?)\s*$")


def _run(args: list[str], timeout: int = 8) -> tuple[int, str]:
    """跑 pinctrl，返回 (returncode, 合并输出)。永不抛异常。"""
    if not _PINCTRL or not os.path.exists(_PINCTRL):
        return 127, f"pinctrl 不存在（找过 {_PINCTRL}）"
    try:
        p = subprocess.run(
            [_PINCTRL, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"pinctrl 超时（>{timeout}s）"
    except FileNotFoundError:
        return 127, f"找不到 pinctrl：{_PINCTRL}"
    except Exception as e:  # noqa: BLE001 —— 感知层不因单点异常整体崩
        return 1, f"{type(e).__name__}: {e}"


def _parse_pins(text: str) -> list[dict]:
    """把 pinctrl 输出解析成结构化引脚列表。解析不了的行走 skipped，不猜。

    只保留真实物理 GPIO（0-53）。pinctrl 还会吐 100+ 的 FWGPIO —— 那是 SoC
    固件内部信号（BT_ON / WL_ON / LAN_RUN…），不对应排针，列出来只会误导。
    """
    pins, skipped = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            skipped.append(line.strip())
            continue
        num, meta, level, comment = m.groups()
        num = int(num)
        if num > _MAX_BCM:
            continue  # FWGPIO，非物理引脚
        parts = meta.split()
        func = parts[0] if parts else "?"
        pull = parts[1] if len(parts) > 1 else "--"
        pins.append({
            "pin": num,
            "func": func,
            "func_label": _FUNC_LABEL.get(func, func),
            "pull": {"pu": "上拉", "pd": "下拉", "--": "无"}.get(pull, pull),
            "level": {"hi": 1, "lo": 0}.get(level, None),
            "comment": comment,
            "reserved": num in RESERVED_PINS,
            "writable": num in GENERAL_PINS,
        })
    if skipped:
        pins.append({"unparsed": skipped})
    return pins


def list_pins(only_general: bool = False) -> str:
    """列出全部引脚状态。only_general=True 时只看可写的通用引脚。"""
    rc, out = _run(["get"])
    if rc != 0:
        return f"✗ 读引脚失败（pinctrl rc={rc}）：{out}"

    pins = _parse_pins(out)
    unparsed = next((p["unparsed"] for p in pins if "unparsed" in p), None)
    pins = [p for p in pins if "unparsed" not in p]
    if only_general:
        pins = [p for p in pins if p["writable"]]

    if not pins:
        return f"✗ pinctrl 输出无一条可解析（原始输出前 200 字）：{out[:200]}"

    lines = [f"树莓派 GPIO 引脚状态（共 {len(pins)} 个{'通用' if only_general else ''}引脚）", ""]
    for p in pins:
        mark = "🔒" if p["reserved"] else ("✎" if p["writable"] else "·")
        lvl = {1: "高", 0: "低", None: "?"}[p["level"]]
        extra = f"  [{RESERVED_PINS[p['pin']]}]" if p["reserved"] else ""
        lines.append(
            f"  {mark} GPIO{p['pin']:<2} {p['func_label']:<4} "
            f"电平={lvl} 拉={p['pull']:<2} {p['comment']}{extra}"
        )
    lines.append("")
    lines.append("  图例：✎=可写（通用 IO）  🔒=专用功能（只读，写会破坏该功能）")
    if unparsed:
        lines.append(f"  ⚠ {len(unparsed)} 行未能解析，已跳过（未猜测）")
    return "\n".join(lines)


def read_pin(pin: int) -> str:
    """读单个引脚的功能与电平。"""
    rc, out = _run(["get", str(pin)])
    if rc != 0:
        return f"✗ 读 GPIO{pin} 失败（rc={rc}）：{out}"
    pins = [p for p in _parse_pins(out) if "pin" in p and p["pin"] == pin]
    if not pins:
        return f"✗ GPIO{pin} 无解析结果（原始：{out[:200]}）"
    p = pins[0]
    lvl = {1: "高 (3.3V)", 0: "低 (0V)", None: "未知"}[p["level"]]
    note = ""
    if p["reserved"]:
        note = f"\n  ⚠ 专用引脚：{RESERVED_PINS[p['pin']]}"
    elif p["writable"]:
        note = "\n  ✓ 通用 IO，可安全读写"
    return (
        f"GPIO{pin}：{p['func_label']} / 电平 {lvl} / 拉电阻 {p['pull']}\n"
        f"  原始：{p['comment']}{note}"
    )


def _guard_write(pin: int) -> str | None:
    """写操作闸门：返回 None 表示放行，否则返回拒绝理由。"""
    if pin in RESERVED_PINS:
        return (
            f"✗ 拒绝写 GPIO{pin} —— 专用功能引脚：{RESERVED_PINS[pin]}\n"
            f"  这类引脚由内核或 HAT 使用，强行写会破坏其功能（GPIO0/1 甚至会让系统认错扩展板）。\n"
            f"  可写的通用 IO：{', '.join(map(str, GENERAL_PINS))}"
        )
    if pin not in GENERAL_PINS:
        return (
            f"✗ 拒绝写 GPIO{pin} —— 不在通用 IO 白名单内。\n"
            f"  可写的通用 IO：{', '.join(map(str, GENERAL_PINS))}"
        )
    # 引脚正被别的驱动占用（alt function）时不许抢
    rc, out = _run(["get", str(pin)])
    if rc == 0:
        pins = [p for p in _parse_pins(out) if "pin" in p and p["pin"] == pin]
        if pins and pins[0]["func"].startswith("a"):
            return (
                f"✗ 拒绝写 GPIO{pin} —— 当前是 {pins[0]['func_label']}（别的驱动正占用）。\n"
                f"  抢过来会让对方功能失效。请先确认该外设已停用。"
            )
    return None


def write_pin(pin: int, value: int, release: bool = False) -> str:
    """驱动输出引脚。release=True 时操作完把引脚放回 input（不悬空）。"""
    deny = _guard_write(pin)
    if deny:
        return deny
    if value not in (0, 1):
        return f"✗ value 必须是 0 或 1，收到 {value!r}"

    drive = "dh" if value == 1 else "dl"
    rc, out = _run(["set", str(pin), "op", drive])
    if rc != 0:
        return f"✗ 写 GPIO{pin} 失败（rc={rc}）：{out}"

    rc2, out2 = _run(["get", str(pin)])
    verify = ""
    if rc2 == 0:
        pins = [p for p in _parse_pins(out2) if "pin" in p and p["pin"] == pin]
        if pins:
            ok = pins[0]["level"] == value and pins[0]["func"] == "op"
            verify = "✓ 已回读确认" if ok else f"⚠ 回读不一致：{pins[0]['comment']}"

    result = f"✓ GPIO{pin} → {'高 (3.3V)' if value else '低 (0V)'}  {verify}"

    if release:
        rc3, _ = _run(["set", str(pin), "ip"])
        result += "\n  " + ("✓ 已放回 input（不悬空）" if rc3 == 0 else f"⚠ 放回 input 失败 rc={rc3}")
    return result


def blink(pin: int, times: int = 3, interval: float = 0.3) -> str:
    """闪灯：pin 高/低交替 times 次。用于可视化确认引脚接线是否正确。"""
    deny = _guard_write(pin)
    if deny:
        return deny
    times = max(1, min(int(times), 20))
    interval = max(0.05, min(float(interval), 2.0))

    done = 0
    for _ in range(times):
        rc, out = _run(["set", str(pin), "op", "dh"])
        if rc != 0:
            _run(["set", str(pin), "ip"])
            return f"✗ 闪灯中断于第 {done + 1} 次（rc={rc}）：{out}"
        time.sleep(interval)
        _run(["set", str(pin), "op", "dl"])
        time.sleep(interval)
        done += 1
    _run(["set", str(pin), "ip"])
    return f"✓ GPIO{pin} 闪烁 {done} 次（{interval}s 间隔），已放回 input"


# ---------------------------------------------------------------- 板载 LED

def _led_path(name: str) -> str | None:
    for cand in (name, name.upper(), name.lower()):
        p = f"/sys/class/leds/{cand}"
        if os.path.isdir(p):
            return p
    return None


def _is_physical_led(path: str) -> bool:
    """物理灯有 device 链；虚拟灯（default-on / mmc0）只是 trigger 目标，没实体。"""
    return os.path.exists(f"{path}/device")


def _led_state(path: str) -> tuple[str, str, bool]:
    """返回 (亮度, 当前触发模式, 是否可写)。"""
    try:
        with open(f"{path}/brightness") as f:
            b = f.read().strip()
    except Exception:  # noqa: BLE001
        b = "?"
    try:
        with open(f"{path}/trigger") as f:
            trig = f.read().strip()
        m = re.search(r"\[(\w[\w-]*)\]", trig)
        cur = m.group(1) if m else "?"
    except Exception:  # noqa: BLE001
        cur = "?"
    return b, cur, os.access(f"{path}/brightness", os.W_OK)


def led_list() -> str:
    """列出板载 LED。物理灯（真能看见亮灭）与虚拟灯（内核触发器）分开列。"""
    base = "/sys/class/leds"
    if not os.path.isdir(base):
        return "✗ 本机无 /sys/class/leds（非树莓派或内核未启用 LED 驱动）"
    names = sorted(os.listdir(base))
    if not names:
        return "✗ /sys/class/leds 为空"

    physical = [n for n in names if _is_physical_led(f"{base}/{n}")]
    virtual = [n for n in names if n not in physical]

    lines = ["板载 LED：", ""]
    if physical:
        lines.append("  物理灯（实体，能看到亮灭）：")
        for n in physical:
            b, cur, w = _led_state(f"{base}/{n}")
            lines.append(
                f"    · {n:<5} 亮度={b} 触发={cur:<10} {'可写' if w else '需 root'}"
            )
    if virtual:
        lines.append("  虚拟灯（内核触发器目标，不是实体灯，别去控制）：")
        lines.append(f"    · {', '.join(virtual)}")
    lines.append("")
    lines.append("  ACT=绿色活动灯（默认指示 SD 卡读写）  PWR=红色电源灯")
    lines.append("  物理灯写 brightness 需 root —— /sys/class/leds/*/brightness 是 root:root 0644")
    return "\n".join(lines)


def led_set(name: str, value: str) -> str:
    """控制板载 LED。value: on/off/blink/heartbeat/default。需 root 写 /sys。"""
    d = _led_path(name)
    if not d:
        return f"✗ 找不到 LED「{name}」。用 action=list 看可用名称（ACT / PWR / mmc0）"

    value = (value or "").strip().lower()
    bright = f"{d}/brightness"
    trig = f"{d}/trigger"

    if value in ("on", "off", "1", "0"):
        # 先脱离内核触发，才能手动控制亮度
        try:
            with open(trig, "w") as f:
                f.write("none")
        except PermissionError:
            return (
                f"✗ 控制 LED「{name}」需要 root —— /sys/class/leds/*/brightness 属主是 root:root 0644。\n"
                f"  两条路：\n"
                f"    ① 单次：sudo sh -c 'echo 1 > /sys/class/leds/{name}/brightness'\n"
                f"    ② 免密：加一条 sudoers 白名单（见 deploy/systemd/README.md）"
            )
        except Exception as e:  # noqa: BLE001
            return f"✗ 写 trigger 失败：{type(e).__name__}: {e}"
        want = 1 if value in ("on", "1") else 0
        try:
            with open(bright, "w") as f:
                f.write(str(want))
        except Exception as e:  # noqa: BLE001
            return f"✗ 写 brightness 失败：{type(e).__name__}: {e}"
        return f"✓ LED {name} → {'亮' if want else '灭'}（trigger 已置 none，脱离内核控制）"

    if value in ("default", "restore"):
        try:
            with open(trig, "w") as f:
                f.write("mmc0" if name.upper() == "ACT" else "default-on")
        except Exception as e:  # noqa: BLE001
            return f"✗ 恢复默认触发失败：{type(e).__name__}: {e}"
        return f"✓ LED {name} 已恢复默认触发"

    if value in ("blink", "heartbeat", "timer", "activity"):
        trig_name = {"blink": "timer", "activity": "mmc0"}.get(value, value)
        try:
            with open(trig, "w") as f:
                f.write(trig_name)
        except Exception as e:  # noqa: BLE001
            return f"✗ 设置触发模式失败：{type(e).__name__}: {e}"
        return f"✓ LED {name} 触发模式 → {trig_name}"

    return f"✗ 不支持的 value：{value}（可用 on/off/blink/heartbeat/activity/default）"


def summary() -> str:
    """一句话硬件概览：芯片、引脚数、LED、外设接口可用性。"""
    rc, out = _run(["get"])
    pins = [p for p in _parse_pins(out) if "pin" in p] if rc == 0 else []
    general = [p for p in pins if p["writable"]]
    reserved = [p for p in pins if p["reserved"]]

    leds = []
    if os.path.isdir("/sys/class/leds"):
        leds = [n for n in sorted(os.listdir("/sys/class/leds"))
                if _is_physical_led(f"/sys/class/leds/{n}")]

    ifaces = []
    for dev, label in (
        ("/dev/gpiomem", "GPIO"), ("/dev/i2c-1", "I2C"),
        ("/dev/spidev0.0", "SPI"), ("/dev/video0", "摄像头"),
        ("/dev/serial0", "串口"), ("/sys/bus/w1/devices", "1-Wire"),
    ):
        ifaces.append(f"{'✓' if os.path.exists(dev) else '✗'} {label}")

    return (
        f"硬件支配面：\n"
        f"  引脚   {len(pins)} 个物理 GPIO（通用可写 {len(general)} / 专用只读 {len(reserved)}）\n"
        f"  LED    {', '.join(leds) if leds else '无'}\n"
        f"  外设   {'  '.join(ifaces)}\n"
        f"  工具   {_PINCTRL}\n"
        f"  可写   {', '.join('GPIO' + str(p['pin']) for p in general)}"
    )
