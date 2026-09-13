"""安卓手机控制技能 —— 通过 ADB 支配 USB 或同网 WiFi 连接的安卓手机。

工具：android（单一分发，action 决定操作）。
实现：基础动作本文件直接封装 adb 子进程；see/page/find/tap_text/batch/ctext/code/sms
等高级动作委托同目录 adb_ui.py（实时层缓存 + 坐标解析），全项目只有这一份实现。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

ADB = shutil.which("adb") or "adb"
_SCREEN_DIR = Path("/home/wxf/dabai/data/android")
_SCREEN_DIR.mkdir(parents=True, exist_ok=True)


def _run(*args, timeout: int = 20) -> tuple[int, str, str]:
    """跑 adb 命令，返回 (returncode, stdout, stderr)。"""
    try:
        p = subprocess.run(
            [ADB, *args], capture_output=True, timeout=timeout,
        )
        # 手机侧文本常混 GBK/UTF-8，硬用 text=True 会整个命令解码失败、内容全丢
        return (p.returncode,
                p.stdout.decode("utf-8", "replace").strip(),
                p.stderr.decode("utf-8", "replace").strip())
    except subprocess.TimeoutExpired:
        return 1, "", f"超时（{timeout}s）"
    except FileNotFoundError:
        return 1, "", f"找不到 adb，请先安装：sudo apt-get install -y adb"


def _target(serial: str | None) -> list[str]:
    return ["-s", serial] if serial else []


def _online() -> list[str]:
    """当前处于 device 状态的序列号（含 USB 与 TCP 两种通道）。"""
    rc, out, _ = _run("devices")
    return [l.split()[0] for l in out.splitlines() if l.strip().endswith("device")]


def _serial(serial: str | None) -> str | None:
    """确定目标设备：显式 serial 优先；USB 与 TCP 双通道并存时优先 USB（更快）。"""
    if serial:
        return serial
    devs = _online()
    usb = [d for d in devs if ":" not in d]
    tcp = [d for d in devs if ":" in d]
    if len(usb) == 1:
        return usb[0]
    if len(usb) > 1:
        return None  # 真有多台 USB 设备，必须显式指定 serial
    return tcp[0] if tcp else None


def _devices() -> str:
    rc, out, _ = _run("devices")
    if rc != 0:
        return f"✗ adb devices 失败：{_}"
    lines = [l for l in out.splitlines() if l.strip()][1:]  # 跳过首行标题
    real = [l for l in lines if l.strip() and not l.startswith("*")]
    if not real:
        return "没有检测到设备。请：USB 连接手机 → 手机开启「开发者选项→USB 调试」→ 首次弹窗点「允许」。"
    return "在线设备：\n" + "\n".join(real)


def _connect(host: str) -> str:
    if not host:
        return "✗ connect 需要 host 参数（如 192.168.1.5:5555）"
    rc, out, err = _run("connect", host, timeout=15)
    msg = (out or err).strip()
    if "connected" in msg or "already" in msg:
        return f"✓ 已连接 {host}\n{msg}"
    return f"✗ 连接失败：{msg}\n（无线前需先 USB 连一次并执行 adb tcpip 5555）"


_HOST_FILE = _SCREEN_DIR / "wifi_host.txt"
_ADB_PORT = 5555


def _cached_hosts() -> list[str]:
    if _HOST_FILE.exists():
        return [l.strip() for l in _HOST_FILE.read_text().splitlines() if l.strip()]
    return []


def _remember_host(host: str) -> None:
    rest = [h for h in _cached_hosts() if h != host][:4]
    _HOST_FILE.write_text("\n".join([host, *rest]) + "\n")


def _local_prefix() -> str | None:
    """本机 IP 的前三段，用于扫 /24 网段。UDP connect 只选路由，不真发包。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip.rsplit(".", 1)[0] if "." in ip else None


def _probe(ip: str, timeout: float = 0.5) -> bool:
    import socket
    try:
        with socket.create_connection((ip, _ADB_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _scan_lan() -> list[str]:
    """并发扫本网段，找出开着 adb 端口的主机。"""
    from concurrent.futures import ThreadPoolExecutor
    prefix = _local_prefix()
    if not prefix:
        return []
    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as ex:
        return [ip for ip, ok in zip(ips, ex.map(_probe, ips)) if ok]


def _auto_connect() -> str:
    """自动连上手机：在线则直接用，否则先试缓存 IP，再扫网段。"""
    devs = _online()
    if devs:
        return "✓ 已在线，无需重连：\n" + "\n".join(devs)

    for host in _cached_hosts():
        _connect(host)
        if _online():
            _remember_host(host)
            return f"✓ 已按记忆连上 {host}"

    hits = _scan_lan()
    for ip in hits:
        host = f"{ip}:{_ADB_PORT}"
        _connect(host)
        if _online():
            _remember_host(host)
            return f"✓ 扫描发现并连上 {host}"

    return ("✗ 没找到手机。逐项检查：手机与树莓派同一 WiFi；开发者选项里"
            "「USB 调试」开着；此前用 USB 执行过一次 adb tcpip 5555"
            "（手机重启后需重做）。\n"
            f"本次扫过 {_local_prefix() or '?'}.0/24，开着 {_ADB_PORT} 端口的主机：{hits or '无'}")


def _do(serial: str | None, *args, timeout: int = 20) -> str:
    tgt = _target(serial)
    rc, out, err = _run(*tgt, *args, timeout=timeout)
    if rc != 0:
        return f"✗ 失败：{(err or out).strip() or '未知错误'}"
    return out or "✓ 完成"


def _tap(serial, x, y) -> str:
    """坐标点击走 adb_ui.tap_xy：agent 注入（ColorOS 上 input tap 应用收不到）
    + 命中校验 + 落空时回最近候选。裸 input tap 三样都没有。"""
    if x is None or y is None:
        return "✗ tap 需要 x/y 坐标"
    if serial:
        os.environ["ADB_SERIAL"] = str(serial)
    try:
        return _ui().tap_xy(int(x), int(y))
    except Exception as e:
        return f"✗ tap 失败：{e}"


def _swipe(serial, x, y, x2, y2, duration) -> str:
    if None in (x, y, x2, y2):
        return "✗ swipe 需要 x/y/x2/y2"
    d = int(duration or 300)
    return _do(serial, "shell", "input", "swipe",
               str(int(x)), str(int(y)), str(int(x2)), str(int(y2)), str(d))


def _longpress(serial, x, y, duration) -> str:
    if x is None or y is None:
        return "✗ longpress 需要 x/y"
    return _do(serial, "shell", "input", "swipe",
               str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(int(duration or 600)))


def _text(serial, text) -> str:
    if not text:
        return "✗ text 需要内容（仅 ASCII，中文需用 shell input text 的 adbkeyboard 方案）"
    return _do(serial, "shell", "input", "text", text)


_KEYS = {
    "home": "3", "back": "4", "menu": "82", "enter": "66",
    "power": "26", "vol_up": "24", "vol_down": "25", "app_switch": "187",
}


def _key(serial, key) -> str:
    if not key:
        return "✗ key 需要按键名"
    code = _KEYS.get(str(key).lower(), key)
    return _do(serial, "shell", "input", "keyevent", str(code))


def _screenshot(serial) -> str:
    tgt = _target(serial)
    ts = time.strftime("%Y%m%d-%H%M%S")
    remote = "/sdcard/_dabai_shot.png"
    local = _SCREEN_DIR / f"shot-{ts}.png"
    rc, out, err = _run(*tgt, "shell", "screencap", "-p", remote)
    if rc != 0:
        return f"✗ 截图失败：{(err or out).strip()}"
    rc, out, err = _run(*tgt, "pull", remote, str(local))
    if rc != 0:
        return f"✗ 拉取截图失败：{(err or out).strip()}"
    try:
        _ui().prune_shots()
    except Exception:
        pass
    # [[IMG:路径]] 是给 harness 的图片注入标记：工具结果带它，模型直接看见像素
    return (f"✓ 截图已保存：{local}\n[[IMG:{local}]]\n"
            "（图给样子不给坐标：要坐标和可点元素用 action=see，别按图猜像素）")


def _dump(serial) -> str:
    tgt = _target(serial)
    rc, out, err = _run(*tgt, "shell", "uiautomator", "dump", "/sdcard/_ui.xml")
    if rc != 0 or "dumped" not in (out + err).lower():
        return f"✗ dump 失败：{(err or out).strip() or '未确认'}"
    rc, out, err = _run(*tgt, "shell", "cat", "/sdcard/_ui.xml")
    if rc != 0 or not out:
        return f"✗ 读取 UI 失败：{(err or out).strip()}"
    return _parse_ui(out)


def _parse_ui(xml: str) -> str:
    """从 uiautomator dump 的 XML 提取可点击/可输入控件 + 中心点坐标。"""
    import re
    nodes = re.findall(r"<node[^>]*>", xml)
    lines: list[str] = []
    for n in nodes:
        text = re.search(r'text="([^"]*)"', n)
        desc = re.search(r'content-desc="([^"]*)"', n)
        res = re.search(r'resource-id="([^"]*)"', n)
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        clickable = 'clickable="true"' in n
        label = (text.group(1) if text and text.group(1) else
                 desc.group(1) if desc and desc.group(1) else
                 res.group(1) if res and res.group(1) else "")
        if not bounds or not label:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        tag = "可点" if clickable else "文本"
        lines.append(f"[{tag}] {label} @({cx},{cy})")
    if not lines:
        return "✗ 未解析出任何带文字/描述的控件"
    return "屏幕控件（中心点坐标）：\n" + "\n".join(lines[:60])


def _start(serial, pkg) -> str:
    if not pkg:
        return "✗ start 需要包名（如 com.tencent.mm）"
    return _do(serial, "shell", "monkey", "-p", pkg, "-c",
               "android.intent.category.LAUNCHER", "1")


def _install(serial, apk) -> str:
    if not apk:
        return "✗ install 需要 apk 路径"
    return _do(serial, "install", apk, timeout=120)


def _unlock(serial) -> str:
    tgt = _target(serial)
    _run(*tgt, "shell", "input", "keyevent", "26")  # power
    time.sleep(0.3)
    return _do(serial, "shell", "input", "keyevent", "82")  # menu 解锁


def _shell(serial, cmd) -> str:
    if not cmd:
        return "✗ shell 需要 cmd"
    return _do(serial, "shell", cmd, timeout=30)


_UI_ACTIONS = {"see", "page", "find", "tap_text", "ctext", "batch", "code", "sms", "scenario", "verify", "annotate"}


_UI_CACHE: dict = {}
_LOCK_CACHE: dict = {}
_SCENE_CACHE: dict = {}
_VERIFY_CACHE: dict = {}


def _ui():
    """延迟导入同目录 adb_ui.py，源文件改了自动重载。

    坐标解析、实时层缓存、batch 编排都只在那边实现一份；技能被加载时
    也不付它的 import 成本。重载是必需的：harness 是常驻进程，模块进了
    sys.modules 就不会再读盘，不查 mtime 会一直跑旧代码。
    """
    import importlib
    import sys
    here = Path(__file__).resolve().parent
    src = here / "adb_ui.py"
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    mod = sys.modules.get("adb_ui")
    if mod is not None:
        cur = getattr(mod, "__file__", "") or ""
        # 实现从 tools/ 搬到了本目录，路径不符的旧模块必须丢掉，否则 reload 会去读旧路径
        if not cur or Path(cur).resolve() != src:
            sys.modules.pop("adb_ui", None)
    import adb_ui
    mtime = src.stat().st_mtime_ns if src.exists() else 0
    if _UI_CACHE.get("mtime") != mtime:
        adb_ui = importlib.reload(adb_ui)
        _UI_CACHE["mtime"] = mtime
    return adb_ui


def _scenario(name: str, n: int) -> str:
    """场景脚本：进程内跑完多步流程（滚屏采集等），只把结果交回来。

    与 _ui() 同样按 mtime 重载：harness 是常驻进程，不重载就一直跑旧代码。
    """
    import importlib
    import sys
    here = Path(__file__).resolve().parent
    src = here / "scenario.py"
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import scenario
    mtime = src.stat().st_mtime_ns if src.exists() else 0
    if _SCENE_CACHE.get("mtime") != mtime:
        scenario = importlib.reload(scenario)
        _SCENE_CACHE["mtime"] = mtime
    if name == "notes":
        return scenario.collect_notes(n)
    return f"✗ 未知场景 {name}（现有：notes）"


def _verify(args: dict) -> str:
    """像素级验证：区域定位 + 颜色判据。与 _ui() 同样按 mtime 重载。"""
    import importlib
    import sys
    here = Path(__file__).resolve().parent
    src = here / "verify.py"
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import verify
    mtime = src.stat().st_mtime_ns if src.exists() else 0
    if _VERIFY_CACHE.get("mtime") != mtime:
        verify = importlib.reload(verify)
        _VERIFY_CACHE["mtime"] = mtime
    return verify.do_verify(
        kw=str(args.get("kw") or ""),
        color=str(args.get("color") or ""),
        tol=int(args.get("tol") or 50),
        label=str(args.get("label") or ""),
        box=str(args.get("box") or ""),
        reset=bool(args.get("reset")),
    )


def _ui_action(a: str, args: dict) -> str:
    ser = args.get("serial")
    if ser:
        os.environ["ADB_SERIAL"] = str(ser)
    kw = str(args.get("kw") or args.get("text") or "")
    if not _online():
        _auto_connect()
    try:
        ui = _ui()
    except Exception as e:
        return f"✗ 加载 adb_ui 失败：{e}"
    try:
        if a == "see":
            return ui.do_see()
        if a == "annotate":
            return ui.do_annotate(int(args.get("n") or 50))
        if a == "page":
            return ui.do_page()
        if a == "find":
            if not kw:
                return "✗ find 需要 kw"
            hits = ui.find_hits(kw)
            if not hits:
                return f"✗ 没找到「{kw}」"
            return "\n".join(hits)
        if a == "tap_text":
            if not kw:
                return "✗ tap_text 需要 kw"
            return ui.do_tap(kw)
        if a == "ctext":
            if not kw:
                return "✗ ctext 需要 text"
            return ui.do_ctext(kw)
        if a == "batch":
            spec = str(args.get("spec") or args.get("cmd") or "")
            if not spec:
                return "✗ batch 需要 spec"
            return ui.do_batch(spec)
        if a == "scenario":
            return _scenario(str(args.get("name") or "notes"), int(args.get("n") or 12))
        if a == "verify":
            return _verify(args)
        if a == "code":
            return ui.do_code()
        if a == "sms":
            return ui.do_sms(int(args.get("n") or 5))
    except Exception as e:
        return f"✗ {a} 执行失败：{e}"
    return f"✗ 不支持的 action：{a}"


def _phone_lock():
    """按 mtime 加载 tools/phone_lock.py —— 与 _ui() 同款。

    锁只能有一份实现：长跑 worker 和主对话用的是同一台手机，各写各的锁等于没锁。
    """
    import importlib
    import sys
    tools = Path(__file__).resolve().parents[2] / "tools"
    src = tools / "phone_lock.py"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    mod = sys.modules.get("phone_lock")
    if mod is not None:
        cur = getattr(mod, "__file__", "") or ""
        if not cur or Path(cur).resolve() != src:
            sys.modules.pop("phone_lock", None)
    import phone_lock
    mtime = src.stat().st_mtime_ns if src.exists() else 0
    if _LOCK_CACHE.get("mtime") != mtime:
        phone_lock = importlib.reload(phone_lock)
        _LOCK_CACHE["mtime"] = mtime
    return phone_lock


def _dispatch(a: str, args: dict) -> str:
    if a in _UI_ACTIONS:
        return _ui_action(a, args)
    ser = _serial(args.get("serial"))
    if ser is None:
        _auto_connect()  # 掉线兜底：后台保活有 65 秒盲区，操作路径上必须自愈一次
        ser = _serial(args.get("serial"))
    if ser is None:
        return "✗ 没有唯一在线设备，请先 adb devices 确认连接（或指定 serial）"
    if a == "tap":
        return _tap(ser, args.get("x"), args.get("y"))
    if a == "swipe":
        return _swipe(ser, args.get("x"), args.get("y"),
                      args.get("x2"), args.get("y2"), args.get("duration"))
    if a == "longpress":
        return _longpress(ser, args.get("x"), args.get("y"), args.get("duration"))
    if a == "text":
        return _text(ser, args.get("text"))
    if a == "key":
        return _key(ser, args.get("key"))
    if a == "screenshot":
        return _screenshot(ser)
    if a == "dump":
        return _dump(ser)
    if a == "start":
        return _start(ser, args.get("pkg"))
    if a == "install":
        return _install(ser, args.get("apk"))
    if a == "unlock":
        return _unlock(ser)
    if a == "shell":
        return _shell(ser, args.get("cmd"))
    return f"✗ 不支持的 action：{a}"


async def execute(tool_name: str, arguments: dict) -> str:
    """harness 约定：dispatch(tool_name, arguments)，action 在 arguments 里。

    除 devices/connect/auto 三个探测动作外全走手机锁：worker 与主对话共用一台
    手机，交叉执行 adb 不是「可能点错」，是必然点错。
    """
    args = arguments or {}
    a = (args.get("action") or "devices").strip().lower()
    if a == "devices":
        return _devices()
    if a == "connect":
        return _connect(args.get("host") or "")
    if a == "auto":
        return _auto_connect()
    owner = os.environ.get("DABAI_AGENT") or "dabai"
    wait = float(os.environ.get("DABAI_PHONE_WAIT", "15"))
    try:
        pl = _phone_lock()
    except Exception as e:
        return _dispatch(a, args) + f"\n⚠ 手机锁加载失败（{e}），本次未互斥"
    with pl.hold(owner, wait=wait) as (ok, info):
        if not ok:
            held = int(time.time() - float(info.get("since") or 0)) if info.get("since") else 0
            return (f"✗ 手机正被「{info.get('owner')}」占用（pid {info.get('pid')}，已 {held}s），"
                    f"本次 {a} 未执行。等它结束再试；要排队更久可设 DABAI_PHONE_WAIT=60。")
        return _dispatch(a, args)
