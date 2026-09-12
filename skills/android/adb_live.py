#!/usr/bin/env python3
"""adb_live —— 手机实时状态层：触摸事件流 + UI 快照缓存。

存在的理由：每次问手机"现在什么页面、有哪些按钮"都要现场 dump（3~11 秒，
播放视频时 uiautomator 还会卡 idle 直接失败）。本脚本常驻后台，把这两件事
变成毫秒级读取：
  - 触摸流：直接读 /dev/input/event*（shell 在 input 组，无需 root）。用户手指
    一动就知道，纯被动、不干扰手机。
  - UI 快照：后台循环 dump 并缓存，调用时读缓存而不是现场等。

用法：
  adb_live.py start           启动后台守护
  adb_live.py stop            停止
  adb_live.py status          运行状态
  adb_live.py now             当前页面 + 可点元素 + 用户最近动作（毫秒级）
  adb_live.py watch [秒]      最近 N 秒用户操作流（默认 30）
  adb_live.py refresh         强制立刻刷新一次快照
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb_ui  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "android"
SNAP = DATA / "live.json"
EVLOG = DATA / "live.log"
PIDF = DATA / "live.pid"
EVLOG_KEEP = 3000

TOUCH_RE = re.compile(r"^\[\s*([\d.]+)\]\s+(\S+)\s+(\S+)\s+(\S+)")
TAP_MS, TAP_PX = 350, 40
SWIPE_PX, LONG_MS = 120, 600
RECONNECT = (
    "import asyncio, importlib.util;"
    "spec = importlib.util.spec_from_file_location('andsk', '/home/wxf/dabai/skills/android/skill.py');"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
    "asyncio.run(m.execute('android', {'action': 'auto'}))"
)


def log_event(rec: dict) -> None:
    with EVLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        if EVLOG.stat().st_size > 400_000:
            lines = EVLOG.read_text(encoding="utf-8").splitlines()[-EVLOG_KEEP:]
            EVLOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def norm_win(raw: str) -> str:
    """dumpsys 给的是 Window{hash u0 pkg/activity}，只留 pkg/activity。"""
    m = re.search(r"([\w.]+/[\w.$]+)", raw)
    return m.group(1) if m else raw


def read_events(seconds: float) -> list[dict]:
    if not EVLOG.exists():
        return []
    cutoff = time.time() - seconds
    out = []
    for line in EVLOG.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("t", 0) >= cutoff:
            out.append(rec)
    return out


def screen_size() -> tuple[int, int]:
    _, out = adb_ui.sh("shell", "wm", "size", timeout=15)
    m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2412)


def find_touch_dev() -> tuple[str, float, float]:
    """返回 (event 节点, x 缩放, y 缩放)。坐标轴量程与屏幕像素不是 1:1（实测 16:1）。"""
    _, out = adb_ui.sh("shell", "getevent", "-pl", timeout=25)
    dev, maxx, maxy = None, 0, 0
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("add device"):
            dev, maxx, maxy = s.split(":", 1)[1].strip(), 0, 0
        elif "ABS_MT_POSITION_X" in s and dev:
            m = re.search(r"max (\d+)", s)
            maxx = int(m.group(1)) if m else 0
        elif "ABS_MT_POSITION_Y" in s and dev and maxx:
            m = re.search(r"max (\d+)", s)
            maxy = int(m.group(1)) if m else 0
            if maxy:
                break
    w, h = screen_size()
    if not dev:
        return "/dev/input/event2", maxx / w if maxx else 16.0, maxy / h if maxy else 16.0
    return dev, (maxx / w if maxx else 16.0), (maxy / h if maxy else 16.0)


def touch_worker(dev: str, sx: float, sy: float, wake: threading.Event, stop: threading.Event) -> None:
    """流式读触摸设备，把每个手势落成一条事件。"""
    cmd = [adb_ui.ADB] + (["-s", adb_ui.SERIAL] if adb_ui.SERIAL else []) + \
          ["exec-out", "getevent", "-lt", dev]
    down, rx, ry, t0 = False, 0, 0, 0.0
    x0, y0, pending = 0, 0, False
    while not stop.is_set():
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            time.sleep(2)
            continue
        try:
            for raw in p.stdout:
                if stop.is_set():
                    break
                m = TOUCH_RE.match(raw.decode("utf-8", "replace"))
                if not m:
                    continue
                _, kind, code, val = m.groups()
                if kind == "EV_ABS" and code == "ABS_MT_POSITION_X":
                    rx = int(val, 16)
                    if pending:
                        x0, pending = rx, False
                elif kind == "EV_ABS" and code == "ABS_MT_POSITION_Y":
                    ry = int(val, 16)
                    if down and not pending and not y0:
                        y0 = ry
                elif kind == "EV_KEY" and code == "BTN_TOUCH":
                    if val == "DOWN":
                        down, t0, pending, y0 = True, time.time(), True, 0
                    elif val == "UP" and down:
                        down = False
                        dur = int((time.time() - t0) * 1000)
                        dist = ((rx - x0) ** 2 + (ry - y0) ** 2) ** 0.5
                        if dist > SWIPE_PX * sx:
                            k = "swipe"
                        elif dur >= LONG_MS:
                            k = "longpress"
                        else:
                            k = "tap"
                        rec = {"t": round(time.time(), 3), "kind": k,
                               "x": int(rx / sx), "y": int(ry / sy), "dur": dur}
                        if k == "swipe":
                            rec["from"] = [int(x0 / sx), int(y0 / sy)]
                        log_event(rec)
                        wake.set()
        except Exception:
            pass
        finally:
            try:
                p.kill()
            except OSError:
                pass
        time.sleep(0.5)


def dump_ui(timeout: int = 9) -> str:
    """无副作用的 UI dump —— adb_ui.dump_raw 失败时会 tap 屏幕（会暂停视频），守护进程不能这么干。
    直接复用 adb_ui.dump_ui：它默认走常驻 agent（0.2~0.35s），比这里的 adb uiautomator
    dump（中位 2.4s）快 9 倍，且不会静默读到上一次的旧屏。"""
    return adb_ui.dump_ui(timeout=timeout)


def snapshot() -> dict | None:
    t0 = round(time.time(), 3)   # ts 取 dump 发起时刻，理由同 adb_ui.snapshot
    win = norm_win(adb_ui.cur_window())
    xml = dump_ui()
    if not xml:
        return None
    w, h = screen_size()
    clickable, texts, seen = [], [], set()
    for n in adb_ui.parse(xml):
        txt = (n["text"] or "").strip()
        desc = (n["desc"] or "").strip()
        key = (txt, desc, n["cx"], n["cy"])
        if key in seen:
            continue
        seen.add(key)
        label = txt or desc
        if not label and not n["clickable"]:
            continue
        item = {"t": label, "c": [n["cx"], n["cy"]],
                "cls": n["cls"].split(".")[-1],
                "id": n["rid"].split("/")[-1]}
        if n["clickable"]:
            clickable.append(item)
        elif label:
            texts.append(item)
    return {"ts": t0, "win": win, "screen": [w, h],
            "clickable": clickable[:60], "texts": texts[:60]}


def write_snap(data: dict) -> None:
    """旧屏不许盖新屏——调用方（settle）和守护并发写同一个 live.json。"""
    try:
        cur = json.loads(SNAP.read_text(encoding="utf-8"))
        if cur.get("ts", 0) > data.get("ts", 0):
            return
    except (OSError, ValueError):
        pass
    tmp = SNAP.with_name(f"{SNAP.stem}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SNAP)
    finally:
        tmp.unlink(missing_ok=True)


def read_snap() -> dict | None:
    try:
        return json.loads(SNAP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    # 单例靠 flock，不靠「查 pid 文件再启动」：后者两步之间有竞态，两个调用方同时
    # 进来会各起一个守护，而 pid 文件只记得最后一个——另一个成孤儿：stop 杀不到、
    # status 看不见，还双倍 dump、各自 unlink 对方的快照。实测就是这么泄漏出两个的。
    # 锁的 fd 活到进程结束（变量一直被 daemon 帧引用），退出即自动释放。
    lock = open(DATA / "live.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return   # 已有守护在跑，安静退出（并发起两个不是错误，是竞态）
    SNAP.unlink(missing_ok=True)   # 旧快照会让调用方以为已经新鲜了
    PIDF.write_text(str(os.getpid()), encoding="utf-8")
    stop, wake = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    dev, sx, sy = find_touch_dev()
    log_event({"t": round(time.time(), 3), "kind": "boot", "dev": dev,
               "scale": [round(sx, 3), round(sy, 3)]})
    threading.Thread(target=touch_worker, args=(dev, sx, sy, wake, stop), daemon=True).start()
    fails, last_reconnect = 0, 0.0
    while not stop.is_set():
        woken = wake.wait(timeout=6)
        wake.clear()
        if stop.is_set():
            break
        if woken:
            time.sleep(0.3)
        snap = snapshot()
        if snap:
            write_snap(snap)
            fails = 0
        else:
            fails += 1
            log_event({"t": round(time.time(), 3), "kind": "dump_fail", "n": fails})
            if fails >= 3 and time.time() - last_reconnect > 60:
                last_reconnect = time.time()
                log_event({"t": round(last_reconnect, 3), "kind": "reconnect"})
                subprocess.Popen([sys.executable, "-c", RECONNECT],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(min(fails * 2, 10))
    try:
        if PIDF.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PIDF.unlink()
    except OSError:
        pass


def ensure_daemon() -> bool:
    """取快照前顺手把守护拉起来（掉线重连后守护可能已死）。"""
    if PIDF.exists():
        try:
            if alive(int(PIDF.read_text().strip())):
                return True
        except (OSError, ValueError):
            pass
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "daemon"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        if PIDF.exists():
            return True
    return False


def fmt_now() -> str:
    snap = read_snap()
    if not snap:
        return "没有快照（UI dump 失败，视频播放中常见）。稍后重试或跑 refresh。"
    age = time.time() - snap["ts"]
    pkg = snap["win"].split(" ")[0]
    lines = [f"前台 {pkg} | 快照 {age:.1f}s 前{' [过期]' if age > adb_ui.STALE_MARK else ''}"
             f" | 屏幕 {snap['screen'][0]}x{snap['screen'][1]}"]
    touches = [e for e in read_events(60) if e.get("kind") in ("tap", "swipe", "longpress")]
    if touches:
        e = touches[-1]
        ago = time.time() - e["t"]
        if e["kind"] == "swipe":
            lines.append(f"用户最近动作 滑动 {e['from']}→[{e['x']},{e['y']}] {e['dur']}ms，{ago:.0f}s 前")
        else:
            lines.append(f"用户最近动作 {e['kind']} ({e['x']},{e['y']}) {e['dur']}ms，{ago:.0f}s 前")
    else:
        lines.append("用户最近 60s 没碰手机")
    if snap["clickable"]:
        lines.append(f"可点 {len(snap['clickable'])} 个：")
        for i, it in enumerate(snap["clickable"], 1):
            lines.append(f"  {i}. {it['t'] or '(无文字)'} @({it['c'][0]},{it['c'][1]}) {it['cls']}")
    if snap["texts"]:
        lines.append("文本：" + " | ".join(it["t"] for it in snap["texts"][:25]))
    return "\n".join(lines)


def fmt_watch(secs: float) -> str:
    evs = read_events(secs)
    if not evs:
        return f"最近 {secs:.0f} 秒无事件"
    out = []
    for e in evs:
        ago = time.time() - e["t"]
        k = e["kind"]
        if k == "boot":
            out.append(f"[{ago:6.0f}s前] 守护启动 {e['dev']} scale={e['scale']}")
        elif k == "dump_fail":
            out.append(f"[{ago:6.0f}s前] UI dump 失败 #{e['n']}")
        elif k == "swipe":
            out.append(f"[{ago:6.0f}s前] 滑动 {e['from']}→[{e['x']},{e['y']}] {e['dur']}ms")
        else:
            out.append(f"[{ago:6.0f}s前] {k} ({e['x']},{e['y']}) {e['dur']}ms")
    return "\n".join(out)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "now"
    if arg == "daemon":
        daemon()
    elif arg == "start":
        if ensure_daemon():
            pid = PIDF.read_text().strip()
            for _ in range(20):
                if read_snap():
                    break
                time.sleep(1)
            snap = read_snap()
            extra = f"，首份快照已就绪（{len(snap['clickable'])} 个可点）" if snap else "，快照稍后生成"
            print(f"守护已启动 pid={pid}{extra}")
        else:
            print("启动失败：pid 文件没出现")
    elif arg == "stop":
        if PIDF.exists():
            pid = int(PIDF.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"已停止 pid={pid}")
            except OSError:
                print("进程已不在，清理 pid 文件")
            PIDF.unlink(missing_ok=True)
        else:
            print("没有在跑")
    elif arg == "status":
        if PIDF.exists() and alive(int(PIDF.read_text().strip())):
            snap = read_snap()
            age = f"{time.time() - snap['ts']:.1f}s 前" if snap else "无"
            n = len(read_events(3600))
            print(f"运行中 pid={PIDF.read_text().strip()} | 快照 {age} | 近 1 小时事件 {n} 条")
        else:
            print("未运行")
    elif arg == "now":
        if not (PIDF.exists() and alive(int(PIDF.read_text().strip()))):
            ensure_daemon()
        if not read_snap():
            for _ in range(12):
                time.sleep(1)
                if read_snap():
                    break
        print(fmt_now())
    elif arg == "watch":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 30
        print(fmt_watch(secs))
    elif arg == "tap":
        kw = sys.argv[2] if len(sys.argv) > 2 else ""
        if not kw:
            print("用法：adb_live.py tap <关键词>")
            return
        snap = read_snap()
        if not snap or time.time() - snap["ts"] > 8:
            s = snapshot()
            if s:
                write_snap(s)
                snap = s
        if not snap:
            print("没有可用快照")
            return
        hits = [it for it in snap["clickable"] + snap["texts"]
                if kw.lower() in it["t"].lower()]
        if not hits:
            print(f"没找到「{kw}」")
            return
        it = hits[0]
        x, y = it["c"]
        adb_ui.sh("shell", "input", "tap", str(x), str(y))
        print(f"已点「{it['t']}」@({x},{y})（命中 {len(hits)} 个）")
    elif arg == "refresh":
        s = snapshot()
        if s:
            write_snap(s)
            print(f"已刷新：{len(s['clickable'])} 个可点 / {len(s['texts'])} 条文本")
        else:
            print("dump 失败（视频播放中 uiautomator 会卡 idle），稍后重试")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
