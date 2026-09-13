#!/usr/bin/env python3
"""手机 UI 自动化小工具 —— 把 dump→解析→点击 收进一次调用。

存在的理由：每一步都用 LLM 往返（看屏幕→想→点）太慢。把"看-想-点"
循环压进本脚本，一次调用完成，省掉 N 次往返。

用法：
  adb_ui.py cur                        当前前台窗口
  adb_ui.py find <关键词>              列出匹配控件
  adb_ui.py see                       一次拿全：窗口 + 可点元素 + 全部文字（走实时层缓存，~0.3s）
  adb_ui.py page                      只抽文字（text + content-desc）
  adb_ui.py tap <关键词>               点第一个匹配控件
  adb_ui.py tap-all <关键词>           点所有匹配控件
  adb_ui.py wait <关键词> [秒]          等到关键词出现（默认 10 秒）
  adb_ui.py seq "<动作;动作;...>"       一次跑多步（见下）
  adb_ui.py xy <x> <y>                 按坐标点
  adb_ui.py swipe <x1> <y1> <x2> <y2> [毫秒]
  adb_ui.py key <键名>
  adb_ui.py text <内容>                输入 ASCII 文本
  adb_ui.py ctext <内容>               输入中文/emoji（走 ADBKeyboard 广播，需先装并选中）
  adb_ui.py ime [adb|restore]         切输入法：adb=切到 ADBKeyboard，restore=还原原输入法
  adb_ui.py shot [路径]                截图（默认 data/android/）
  adb_ui.py sms [条数]                 读最新短信
  adb_ui.py code                       提取最新验证码
  adb_ui.py screen                     屏幕尺寸/前台包名

seq 语法（分号分隔，一次调用跑完）：
  tap:同意 | wait:首页 | swipe:540,1600,540,500,200 | sleep:2 | shot | text:hello

环境变量 ADB_SERIAL 可指定设备。
"""
from __future__ import annotations

import atexit
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ADB = shutil.which("adb") or "adb"
SERIAL = os.environ.get("ADB_SERIAL") or None

_SER_CACHE: tuple[float, str | None] = (0.0, None)


def _serial() -> str | None:
    """这次要用的设备号。USB 与无线同时在列时，不带 -s 的 adb 会直接报
    "more than one device/emulator"，于是所有 shell 动作静默失败——see 的窗口行
    显示 "?"、input tap 不生效，都是这一个原因（2026-09-13 实测）。
    优先 USB：无线那条在手机重启后必失效。环境变量优先于模块常量，
    skill.py 是运行时写 ADB_SERIAL 的，import 期读到的常量看不到。
    """
    s = os.environ.get("ADB_SERIAL") or SERIAL
    if s:
        return s
    global _SER_CACHE
    now = time.time()
    if now - _SER_CACHE[0] < 10:
        return _SER_CACHE[1]
    picked = None
    try:
        p = subprocess.run([ADB, "devices"], capture_output=True, timeout=8)
        devs = [ln.split()[0] for ln in p.stdout.decode("utf-8", "replace").splitlines()[1:]
                if len(ln.split()) >= 2 and ln.split()[1] == "device"]
        usb = [d for d in devs if ":" not in d]
        picked = (usb or devs or [None])[0]
    except Exception:
        picked = None
    _SER_CACHE = (now, picked)
    return picked


SHOT_DIR = Path("/home/wxf/dabai/data/android")
REMOTE_XML = "/sdcard/_adb_ui.xml"
IME_ADB = "com.android.adbkeyboard/.AdbIME"
IME_USER = "com.baidu.input_oppo/.ImeService"  # 存盘丢了时的兼底：本机原输入法
IME_STATE = Path("/home/wxf/dabai/data/android/ime_before.txt")
# 非空 = 我们临时借用了 ADBKeyboard，里面存着该还回去的那个；atexit 据此兜底
_IME_HELD = ""

# 缓存新鲜度上限（秒）：按「用途」分级，一处定义，别再在各函数里散着写数字。
# 定这些数的依据：agent dump 一次 0.2~0.35s，重采很便宜；越不能出错的用途给得越紧。
#   tap  —— 照着坐标点。点错页面代价最大，只认 2.5s 内，超了宁可现场重采
#   base —— batch 开头取基线，只喂给指纹比对，不落点
#   look —— 看屏 / 找控件 / 抽文字。读到 8 秒前的屏通常仍是用户当下看到的屏
#   fp   —— 指纹比对只想要「最新缓存」，越新越好，但不值得为它强制重采
#   any  —— 只要缓存存在就行，新不新由调用方按 ts 单调性自己判
# 不吃缓存的路：do_wait 等关键词、do_tap 缓存不新鲜时，都走现场 dump。
FRESH = {"tap": 2.5, "base": 5.0, "look": 8.0, "fp": 600.0, "any": 600.0}
STALE_MARK = 20.0   # 超过它就标 [过期]：给用户看的「这屏可能不是现在」


def sh(*args, timeout: int = 25) -> tuple[int, str]:
    s = _serial()
    cmd = [ADB] + (["-s", s] if s else []) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"超时（{timeout}s）"
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def shb(*args, timeout: int = 25) -> bytes:
    s = _serial()
    cmd = [ADB] + (["-s", s] if s else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return p.stdout


AGENT_PORT = 9008
U2_JAR = "/data/local/tmp/u2.jar"
AGENT_LOG = "/data/local/tmp/u2/agent.log"


def agent_rpc(method: str, params: list | None = None, timeout: int = 15):
    """直连手机端常驻 agent 的 JSON-RPC，返回 result 字段。

    不用 uiautomator2 库的理由：它把 agent 挂在 adb shell 连接上，shell 一断
    agent 就死，每次 connect 都要重付 1.4s 启动成本。这里 agent 用 nohup 脱离
    shell 常驻，本机走裸 HTTP，单次 dump 只剩一个往返（实测 206~322ms）。
    """
    conn = http.client.HTTPConnection("127.0.0.1", AGENT_PORT, timeout=timeout)
    try:
        conn.request(
            "POST", "/jsonrpc/0",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                        "params": params or []}),
            {"User-Agent": "uiautomator2", "Accept-Encoding": "",
             "Content-Type": "application/json"},
        )
        data = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:120])
    return data.get("result")


def agent_launch() -> bool:
    """重建 agent（jar 不在就先部署），就绪返回 True。

    探测必须用 dumpWindowHierarchy 本身：agent 的 HTTP 服务先起来、UiAutomation
    后连上，中间那段调任何方法都返回 -32601 method not found（我曾拿 windowSize
    当探针，它压根不是 agent 的方法名，于是轮询 10 秒全白等、再回落 adb，单次
    调用拖到 60s 超时）。实测从启动到真能 dump 只要 0.97s，所以轮询间隔取 0.4s。
    也别用 `netstat | grep 9008` 判就绪——手机上有几十条连向 9008 的 TIME_WAIT
    残留，会把没起来的 agent 误判成已就绪。
    """
    if sh("shell", "ls", U2_JAR)[0] != 0:
        try:
            import uiautomator2 as u2
            u2.connect().dump_hierarchy()
        except Exception:
            return False
    sh("forward", f"tcp:{AGENT_PORT}", f"tcp:{AGENT_PORT}")
    sh("shell", "pkill", "-9", "-f", "uia2")
    time.sleep(0.3)
    sh("shell", f"nohup sh -c 'CLASSPATH={U2_JAR} app_process / com.wetest.uia2.Main"
                f" -p {AGENT_PORT}' >{AGENT_LOG} 2>&1 </dev/null &")
    for i in range(10):
        time.sleep(0.4)
        if _agent_try():
            return True
    return False


def _agent_try() -> str:
    try:
        xml = agent_rpc("dumpWindowHierarchy", [False, 50])
        return xml if xml and "<node" in xml else ""
    except Exception:
        return ""


def _dump_via_agent() -> str:
    """走常驻 agent 取 XML，失败返回空串。

    热路径就一次 RPC（实测中位 322ms），不做事前探测；这一发没成才重建 agent。

    """
    xml = _agent_try()
    if xml:
        return xml
    if not agent_launch():
        return ""
    return _agent_try()


def dump_raw(retries: int = 3) -> str:
    """取 UI XML。返回空串 = 这次真的没取到，调用方不许当成功用。

    三个坑实测踩过（这台 OPPO/Android 15 上 adb dump 有 60% 概率失败）：
    ① dump 进程会被系统杀（rc=137），失败时 XML 文件根本没更新——直接 cat 读到
       的是上一次的旧屏，而且里面带着 <node>、parse 得出完整控件表，看起来完全
       正常。所以每轮先 rm 旧文件：删干净还读不到 <node>，才说明这次真失败。
    ② 残留 uiautomator 进程会累积（实测抓到 2 个卡在 futex_wait 不退），后一次
       dump 更容易被杀，所以失败时顺手清一遍。
    ③ 失败必须重试、不许将就：加重试后 6/6 成功，不加则 6/10 静默读到旧屏。
    """
    xml = _dump_via_agent()
    if xml:
        return xml
    # 兜底前必须先让 agent 交出 UiAutomation：agent 常驻时它一直握着连接，
    # 新起的 uiautomator 进程抢不到、必然被杀（实测 3 次重试全失败，9 秒返回空）。
    sh("shell", "pkill", "-9", "-f", "uia2")
    time.sleep(0.3)
    for attempt in range(retries):
        sh("shell", "rm", "-f", REMOTE_XML)
        sh("shell", "uiautomator", "dump", REMOTE_XML)
        xml = shb("exec-out", "cat", REMOTE_XML).decode("utf-8", "replace")
        if "<node" in xml:
            return xml
        if attempt < retries - 1:
            sh("shell", "pkill", "-9", "-f", "uiautomator")
            time.sleep(0.3)
    sh("shell", "input", "tap", "540", "1200")
    time.sleep(0.6)
    sh("shell", "rm", "-f", REMOTE_XML)
    sh("shell", "uiautomator", "dump", REMOTE_XML)
    xml = shb("exec-out", "cat", REMOTE_XML).decode("utf-8", "replace")
    return xml if "<node" in xml else ""


def parse(xml: str) -> list[dict]:
    """解析成扁平控件表，带父链（用于向上找可点击祖先）。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    parent = {c: p for p in root.iter() for c in p}
    out = []
    for el in root.iter("node"):
        b = el.get("bounds") or ""
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        chain = []
        node = el
        while node is not None:
            chain.append(node)
            node = parent.get(node)
        out.append({
            "text": el.get("text") or "",
            "desc": el.get("content-desc") or "",
            "rid": el.get("resource-id") or "",
            "cls": el.get("class") or "",
            "clickable": el.get("clickable") == "true",
            "longclick": el.get("long-clickable") == "true",
            "scrollable": el.get("scrollable") == "true",
            "editable": el.get("editable") == "true",
            "checkable": el.get("checkable") == "true",
            "checked": el.get("checked") == "true",
            "enabled": el.get("enabled") != "false",
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "b": [x1, y1, x2, y2],
            "chain": chain,
        })
    return out


def nodes_now() -> list[dict]:
    """取控件表：agent 一次 RPC（实测中位 322ms）拿 XML 再解析。"""
    return parse(dump_raw())


def hit(node: dict) -> tuple[int, int]:
    """点击坐标：控件本身可点就用它，否则向上找最近的可点击祖先。"""
    for el in node["chain"]:
        if el.get("clickable") == "true":
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", el.get("bounds") or "")
            if m:
                a, b, c, d = map(int, m.groups())
                return (a + c) // 2, (b + d) // 2
    return node["cx"], node["cy"]


def kw_rank(kw: str, label: str) -> tuple:
    """匹配优先级：完全相等 > 前缀 > 子串；同级短标签优先（越短越精确）。
    存在的理由：实测 tap「我」落到标题含「…来自我是天才…」的笔记卡片上——子串匹配
    先命中谁点谁，界面顺序一变就点错目标。"""
    k, t = kw.lower(), (label or "").lower()
    if t == k:
        return (0, len(t))
    if t.startswith(k):
        return (1, len(t))
    return (2, len(t))


def match(nodes: list[dict], kw: str) -> list[dict]:
    kw_l = kw.lower()
    hits = [n for n in nodes
            if kw_l in n["text"].lower() or kw_l in n["desc"].lower()
            or kw_l in n["rid"].lower()]
    hits.sort(key=lambda n: kw_rank(kw, (n["text"] or n["desc"]).strip()))
    return hits


def cur_window() -> str:
    _, out = sh("shell", "dumpsys", "window")
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.split("=", 1)[-1].strip()
    return "?"


def _agent_click(x: int, y: int) -> bool:
    """走 agent 的 UiAutomation 注入点击，失败返回 False。"""
    try:
        return bool(agent_rpc("click", [int(x), int(y)], timeout=8))
    except Exception:
        return False


def near_hits(x: int, y: int, n: int = 3) -> list[str]:
    """离 (x,y) 最近的可点元素——坐标点空了用来纠偏。

    点空的现场只有一句「无元素覆盖」：知道点空了，但不知道本来该点哪。
    盲猜坐标的代价是整轮白跑（实测 xy 968,168 落空后又要 see 一轮），
    把最近的候选顺手带回去，一次调用就能改判成 tap_text。"""
    s = fresh_snap(FRESH["tap"])
    if not s:
        return []
    out = []
    for it in s.get("clickable") or []:
        t = (it.get("t") or "").strip()
        c = it.get("c") or []
        if not t or len(c) != 2:
            continue
        d = int(((c[0] - x) ** 2 + (c[1] - y) ** 2) ** 0.5)
        out.append((d, f"「{t[:20]}」@({c[0]},{c[1]}) 距 {d}px"))
    out.sort(key=lambda p: p[0])
    return [s for _, s in out[:n]]


def tap_xy(x: int, y: int) -> str:
    """坐标点击。优先 agent 注入，回落 adb input tap。

    ColorOS 上 `input tap` 注入的事件在 InputDispatcher 里是 targetUid=<not set>：
    DOWN/UP 都发出去了，logcat 也能看到 MotionEvent，但应用收不到（实测点拼多多
    商家版「答题领流量」三次全无反应，换 agent click 一次就进去了）。keyevent 不受
    影响，所以只有坐标点击需要走这条路。
    """
    tag = hit_label(x, y)
    if tag:
        tail = f" → 命中「{tag[:24]}」"
    else:
        near = near_hits(x, y)
        tail = " → ⚠ 该坐标没有元素覆盖（大概率点空）"
        tail += (("；最近的可点：" + "，".join(near) + " —— 改用 tap_text 按文字点")
                 if near else "；当前页取不到可点元素，先 see 再决定点哪")
    if _agent_click(x, y):
        return f"点击 ({x},{y}){tail}"
    sh("shell", "input", "tap", str(x), str(y))
    return f"点击 ({x},{y}){tail}"


def hit_label(x: int, y: int) -> str:
    """坐标落在哪个可点元素上——回答「这一下到底按在什么上面」。

    裸坐标点击看不见落点：点歪了和点了不生效，现象完全一样（界面纹丝不动），
    没有落点信息就只能靠猜。取面积最小的命中元素：父容器总是更大，最内层那个
    才是真正被按的东西。"""
    s = fresh_snap(FRESH["tap"])
    if not s:
        return ""
    best, best_area = "", None
    for it in s.get("clickable") or []:
        b = it.get("b") or []
        if len(b) != 4:
            continue
        l, t, r, bo = b
        if l <= x <= r and t <= y <= bo:
            area = (r - l) * (bo - t)
            if best_area is None or area < best_area:
                best_area, best = area, it.get("t") or ""
    if best:
        return best
    for it in s.get("texts") or []:
        b = it.get("b") or []
        if len(b) != 4:
            continue
        l, t, r, bo = b
        if l <= x <= r and t <= y <= bo:
            area = (r - l) * (bo - t)
            if best_area is None or area < best_area:
                best_area, best = area, it.get("t") or ""
    return f"{best}（文字节点）" if best else ""


def do_tap(kw: str, all_hits: bool = False) -> str:
    if not all_hits:
        s = fresh_snap(FRESH["tap"], check_win=True)
        if s:
            quick = [i for i in (s.get("clickable") or [])
                     if kw.lower() in i["t"].lower()]
            if quick:
                quick.sort(key=lambda i: kw_rank(kw, i["t"]))
                x, y = quick[0]["c"]
                tap_xy(x, y)
                return (f"✓ 点了「{quick[0]['t'][:24]}」@({x},{y})"
                        f"（缓存命中 {len(quick)} 个）")
    nodes = nodes_now()
    hits = match(nodes, kw)
    if not hits:
        return f"✗ 没找到「{kw}」"
    if not all_hits:
        hits = hits[:1]
    done = []
    for n in hits:
        x, y = hit(n)
        tap_xy(x, y)
        done.append(f"({x},{y})")
        if len(hits) > 1:
            time.sleep(0.4)
    label = hits[0]["text"] or hits[0]["desc"] or hits[0]["rid"]
    return f"✓ 点了「{label[:24]}」× {len(done)} → {' '.join(done)}"


def do_wait(kw: str, timeout: float = 10.0) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if match(nodes_now(), kw):
            return f"✓ 「{kw}」已出现（{time.time() - t0:.1f}s）"
        time.sleep(0.7)
    return f"✗ 等 {timeout}s 没等到「{kw}」"


SHOT_KEEP = 12


def prune_shots(keep: int = SHOT_KEEP) -> int:
    """截图/标注图只有最近几步有用，留着白占 SD 卡：只保留最新 keep 张。"""
    try:
        files = sorted(
            list(SHOT_DIR.glob("shot-*.png")) + list(SHOT_DIR.glob("annot-*.png")),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
    except OSError:
        return 0
    n = 0
    for f in files[keep:]:
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def do_shot(path: str | None = None) -> str:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = Path(path) if path else SHOT_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    data = shb("exec-out", "screencap", "-p", timeout=40)
    p.write_bytes(data)
    prune_shots()
    return f"✓ 截图 {len(data)} 字节 → {p}"


# adb shell 会把参数用空格拼成一条命令发给手机，带空格的参数必须自带引号
SMS_QUERY = ("content query --uri content://sms/inbox "
             "--projection address:body:date --sort 'date DESC'")


def do_sms(n: int = 5) -> str:
    _, out = sh("shell", SMS_QUERY)
    rows = re.findall(r"Row: \d+ address=(.*?), body=(.*?), date=(\d+)", out, re.S)
    if not rows:
        return f"✗ 读不到短信（{out[:120]}）"
    lines = []
    for addr, body, ts in rows[:n]:
        t = time.strftime("%m-%d %H:%M", time.localtime(int(ts) / 1000))
        lines.append(f"[{t}] {addr}：{body.strip()[:110]}")
    return "\n".join(lines)


def do_code(n: int = 6) -> str:
    """从最新短信里抓验证码；短信里没有就翻通知栏明文（短信通知同样带验证码）。"""
    _, out = sh("shell", SMS_QUERY)
    rows = re.findall(r"Row: \d+ address=(.*?), body=(.*?), date=(\d+)", out, re.S)
    for addr, body, ts in rows[:n]:
        t = time.strftime("%m-%d %H:%M", time.localtime(int(ts) / 1000))
        ctx = re.search(r"(?:验证码|校验码|动态码|登录码|口令)[^0-9]{0,12}(\d{4,8})", body)
        if ctx:
            return f"✓ 验证码 {ctx.group(1)}｜{addr}｜{t}"
    _, notes = sh("shell", "dumpsys notification --noredact | grep -aE 'android.text=|tickerText='")
    for m in re.finditer(r"(?:android\.text|tickerText)=String \((.{0,160}?)\)", notes):
        ctx = re.search(r"(?:验证码|校验码|动态码|登录码|口令)[^0-9]{0,12}(\d{4,8})", m.group(1))
        if ctx:
            return f"✓ 验证码 {ctx.group(1)}（来自通知栏）"
    return f"✗ 最新 {n} 条短信和通知栏都没找到验证码"


def do_seq(spec: str) -> str:
    log = []
    for step in [s.strip() for s in spec.split(";") if s.strip()]:
        name, _, arg = step.partition(":")
        name, arg = name.strip().lower(), arg.strip()
        if name == "tap":
            log.append(do_tap(arg))
        elif name == "wait":
            log.append(do_wait(arg))
        elif name == "sleep":
            time.sleep(float(arg or 1))
            log.append(f"sleep {arg}s")
        elif name == "shot":
            log.append(do_shot(arg or None))
        elif name == "text":
            sh("shell", "input", "text", arg)
            log.append(f"输入 {arg}")
        elif name == "ctext":
            log.append(do_ctext(arg))
        elif name == "key":
            sh("shell", "input", "keyevent", arg)
            log.append(f"按键 {arg}")
        elif name == "swipe":
            nums = [p.strip() for p in arg.split(",")]
            if len(nums) >= 4:
                dur = nums[4] if len(nums) > 4 else "300"
                sh("shell", "input", "swipe", nums[0], nums[1], nums[2], nums[3], dur)
                log.append(f"滑 {arg}")
        elif name == "xy":
            x, y = arg.split(",")
            log.append(tap_xy(int(x), int(y)))
        else:
            log.append(f"✗ 未知动作 {name}")
    return "\n".join(log)


def _xy_args(rest: list[str]) -> tuple[int, int]:
    """xy 允许写 "540 1800"，也允许 "540,1800"。"""
    if len(rest) == 1 and "," in rest[0]:
        rest = rest[0].split(",")
    return int(rest[0]), int(rest[1])


def _looks_xy(rest: list[str]) -> bool:
    """batch 里 tap 后面直接跟坐标就按坐标点，省得为「写 tap 还是写 xy」白撞一次。"""
    if len(rest) == 2 and all(re.fullmatch(r"\d+", x) for x in rest):
        return True
    return len(rest) == 1 and bool(re.fullmatch(r"\d+,\d+", rest[0]))


def do_launch(pkg: str, cold: bool = False, timeout: float = 10.0) -> str:
    """跨 app 编排的入口：monkey 按 LAUNCHER 意图拉起，不依赖 activity 名。
    cold 先 force-stop——热启动会停在上次那个页面，读屏结果不可预期。
    必须等目标包名真的到前台再返回，否则后面的 see 读到的是上一个 app 的旧屏
    （缓存 8 秒内都算新鲜，这是跨 app 批量最容易骗自己的坑）。判前台用 cur_window
    直查（132ms），不读缓存——缓存可能是启动前写的，等它自然过期最慢要 6 秒。"""
    if not pkg:
        return "✗ start 需要包名（如 com.tencent.mm）"
    if cold:
        sh("shell", "am", "force-stop", pkg)
        time.sleep(0.5)
    sh("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pkg in norm_win(cur_window()):
            settle("", timeout=1.5)   # 窗口到了不等于内容画好了，等界面稳下来再写缓存
            return f"✓ {pkg} 已在前台（{time.time() - t0:.1f}s）"
        time.sleep(0.25)
    return f"⚠ {pkg} 未在 {timeout:.0f}s 内到前台，当前 {cur_window()}"


def do_stop(pkg: str = "") -> str:
    """force-stop 清场。不传包名就停当前前台那个。"""
    target = pkg or norm_win(cur_window()).split("/")[0]
    sh("shell", "am", "force-stop", target)
    time.sleep(1.0)
    return f"✓ 已停 {target}"


_VERIFY_MTIME: dict = {}


def _do_verify(rest: list) -> str:
    """batch 子命令 verify：verify <资源名|x1,y1,x2,y2> [#色值] [--reset]。
    verify 模块的 mtime 重载在这里再做一遍：batch 不经过 skill.py 的 _verify()。"""
    import importlib
    import verify as VM
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify.py")
    m = os.path.getmtime(p) if os.path.exists(p) else 0
    if _VERIFY_MTIME.get("m") != m:
        VM = importlib.reload(VM)
        _VERIFY_MTIME["m"] = m
    reset = "--reset" in rest
    rest = [x for x in rest if not x.startswith("--")]
    box = rest[0] if rest and re.match(r"^[\d,]+$", rest[0]) else ""
    kw = "" if box else (rest[0] if rest else "")
    color = next((x for x in rest[1:] if x.startswith("#")), "")
    return VM.do_verify(kw=kw, box=box, color=color, reset=reset)


def do_batch(spec: str) -> str:
    """整批只借一次 ADBKeyboard：逐条切会每条多付 ~1.5s，
    且中途异常会把用户键盘留在 ADBKeyboard 上。用完必还。"""
    global _IME_HELD
    steps = [s for s in (x.strip() for x in spec.split(";")) if s]
    borrow = any(s.split()[0].lower() == "ctext" for s in steps)
    if borrow and "adbkeyboard" not in (_c0 := _ime_get()):
        _IME_HELD = _c0 or IME_USER
        IME_STATE.parent.mkdir(parents=True, exist_ok=True)
        IME_STATE.write_text(_IME_HELD, encoding="utf-8")
        _ime_set(IME_ADB, wait=0.6)
    try:
        return _do_batch_impl(spec)
    finally:
        if _IME_HELD:
            _o, _IME_HELD = _IME_HELD, ""
            _ime_set(_o, wait=0)


def _do_batch_impl(spec: str) -> str:
    """一条命令跑完整串动作：分号分隔，每步一个子命令。
    存在的理由：每发起一次调用都要过一次 LLM 往返，把 N 步压进一次进程启动才是真的省。
    示例：batch "see;tap 推荐;see;xy 540,1800"
    动作类步骤后自动等 1.5s，让实时层把新界面 dump 进缓存，下一条 see 读到的是新状态。"""
    actions = {
        "see": lambda r: do_see(),
        "page": lambda r: do_page(),
        "find": lambda r: "\n".join(find_hits(" ".join(r))) or "✗ 无匹配",
        "cur": lambda r: cur_window(),
        "screen": lambda r: do_screen(),
        "tap": lambda r: (tap_xy(*_xy_args(r)) if _looks_xy(r) else do_tap(" ".join(r))),
        "tap_text": lambda r: do_tap(" ".join(r)),
        "tap-all": lambda r: do_tap(" ".join(r), all_hits=True),
        "annotate": lambda r: do_annotate(int(r[0]) if r else 50),
        "wait": lambda r: do_wait(r[0], float(r[1]) if len(r) > 1 else 10.0),
        "xy": lambda r: tap_xy(*_xy_args(r)),
        "key": lambda r: (sh("shell", "input", "keyevent", r[0]), f"✓ 按键 {r[0]}")[1],
        "text": lambda r: (sh("shell", "input", "text", r[0]), f"✓ 输入 {r[0]}")[1],
        "ctext": lambda r: do_ctext(" ".join(r)),
        "ime": lambda r: do_ime(r[0] if r else "adb"),
        "swipe": lambda r: (sh("shell", "input", "swipe", *r[:4],
                               r[4] if len(r) > 4 else "300"), f"✓ 滑动 {' '.join(r)}")[1],
        "sleep": lambda r: (time.sleep(float(r[0])), f"等 {r[0]}s")[1],
        "shot": lambda r: do_shot(r[0] if r else None),
        "sms": lambda r: do_sms(int(r[0]) if r else 5),
        "code": lambda r: do_code(),
        "start": lambda r: do_launch(r[0] if r else "", cold="-c" in r),
        "stop": lambda r: do_stop(r[0] if r else ""),
        "shell": lambda r: sh("shell", *r)[1].strip()[:2000] or "ok",
        "verify": lambda r: _do_verify(r),
    }
    moves = {"tap", "tap_text", "tap-all", "xy", "key", "text", "ctext", "swipe", "stop", "shell"}
    out = []
    base = fresh_snap(FRESH["base"])
    if base is None:
        base = snapshot()
        if base:
            write_snap(base)   # 基线必须当场采：拿 5 秒前的缓存当基线，会把「动作前的屏」误判成「动作后的新屏」
    fp = snap_fp(base) if base else ""
    for i, step in enumerate([s for s in (x.strip() for x in spec.split(";")) if s], 1):
        parts = step.split()
        name, rest = parts[0].lower(), parts[1:]
        act = actions.get(name)
        try:
            if act:
                body = act(rest)
            else:
                import difflib
                near = difflib.get_close_matches(name, list(actions), n=3)
                body = (f"✗ 未知动作 {name}；可用：" + "/".join(actions)
                        + (f"（是不是想写 {'/'.join(near)}？）" if near else ""))
        except Exception as e:
            body = f"✗ {type(e).__name__}: {e}"
        out.append(f"[{i}] {step}\n    " + (body or "ok").replace("\n", "\n    "))
        if name in moves:
            before = fp
            fp = settle(fp)
            if before:
                out[-1] += ("\n    ↳ 界面已变化" if fp != before
                            else "\n    ↳ 界面未变化：这步很可能没生效")
    return "\n".join(out)



def _ime_get() -> str:
    _, cur = sh("shell", "settings", "get", "secure", "default_input_method")
    return cur.strip()


def _ime_set(target: str, wait: float = 1.0) -> str:
    sh("shell", "ime", "enable", target)
    sh("shell", "ime", "set", target)
    if wait:
        time.sleep(wait)
    return _ime_get()


def _ime_peek_orig() -> str:
    """我们切走之前用户用的是哪个：优先存盘，其次兜底常量。"""
    try:
        return IME_STATE.read_text(encoding="utf-8").strip() or IME_USER
    except OSError:
        return IME_USER


def _ime_restore_quietly() -> None:
    """atexit 兜底：进程崩在广播中途也不能把用户的输入法留在 ADBKeyboard 上。"""
    global _IME_HELD
    if not _IME_HELD:
        return
    orig, _IME_HELD = _IME_HELD, ""
    try:
        if _ime_get().startswith("com.android.adbkeyboard"):
            _ime_set(orig, wait=0)
    except Exception:
        pass


atexit.register(_ime_restore_quietly)


def do_ime(want: str = "adb") -> str:
    """显式切输入法（长期停留，直到 restore）。ctext 不走这里——它用完即还。
    切之前先把原输入法存盘，restore 才还得回去。"""
    cur = _ime_get()
    if want in ("adb", "on"):
        if cur and "adbkeyboard" not in cur:
            IME_STATE.parent.mkdir(parents=True, exist_ok=True)
            IME_STATE.write_text(cur, encoding="utf-8")
        target = IME_ADB
    elif want in ("restore", "off"):
        target = _ime_peek_orig()
    else:
        target = want
    now = _ime_set(target)
    return f"输入法 {cur or '?'} → {now or '?'}"


def do_ctext(text: str) -> str:
    """中文/emoji 输入。input text 只吃 ASCII，Unicode 走 ADBKeyboard 的广播通道。
    远端 shell 会把参数再解析一次，所以带空格的文本得自己套上单引号。
    ADBKeyboard 只在广播那一瞬需要，用完立刻还回用户原来的输入法——
    常驻会让手机上一敲键盘就是空的 ADB 输入框，等于把人家键盘搞坏了。"""
    global _IME_HELD
    cur = _ime_get()
    orig = ""
    mine = False                      # 只有本次自己借的才负责还；batch 己借的交给 batch 统一还
    if _IME_HELD:
        orig = _IME_HELD              # batch 已整批借走，本条不再反复切
    elif "adbkeyboard" not in cur:
        orig = cur or IME_USER
        IME_STATE.parent.mkdir(parents=True, exist_ok=True)
        IME_STATE.write_text(orig, encoding="utf-8")
        if "adbkeyboard" not in _ime_set(IME_ADB, wait=0.6):
            _ime_set(orig, wait=0)
            return f"✗ 切不到 ADBKeyboard（当前 {cur or '?'}），已还原，中文输入失败"
        _IME_HELD = orig
        mine = True
    try:
        quoted = "'" + text.replace("'", "'\\''") + "'"
        _, out = sh("shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
                    "--es", "msg", quoted)
        ok = "result=0" in out
    finally:
        if mine:
            _IME_HELD = ""
            _ime_set(orig, wait=0)
    back = "（输入法已还原）" if mine else ""
    return f"{'✓' if ok else '✗'} 输入「{text}」{back}" + ("" if ok else f"（{out[:80]}）")


SNAP = Path("/home/wxf/dabai/data/android/live.json")
_SIZE: tuple[int, int] = (0, 0)


def dump_ui(timeout: float = 2.5) -> str:
    """无副作用的 UI dump（不给 dump_raw 兜底那记 tap——它会暂停视频，刷新/守护路径不能有副作用）。
    默认走常驻 agent（实测 0.2~0.35s，是 adb uiautomator dump 中位 2.4s 的 1/9），失败才回落 adb。"""
    xml = _dump_via_agent()
    if xml:
        return xml
    try:
        sh("shell", "uiautomator", "dump", "--compressed", REMOTE_XML, timeout=timeout)
        xml = shb("exec-out", "cat", REMOTE_XML, timeout=timeout).decode("utf-8", "replace")
    except Exception:
        return ""
    return xml if "<node" in xml else ""


def screen_size() -> tuple[int, int]:
    """屏幕尺寸不会变，缓存住——snapshot 每次调它白花一次 adb 往返（~100ms）。"""
    global _SIZE
    if _SIZE[0]:
        return _SIZE
    _, out = sh("shell", "wm", "size")
    m = re.search(r"(\d+)x(\d+)", out)
    _SIZE = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return _SIZE


SNAP_V = 2  # 快照结构版本；字段一变就 +1，旧缓存自动作废

# 点下去会发生什么：属性是确定的，文字词表是推断（输出带 ≈）。
_EFFECT_RULES = (
    (r"^(已领取|已领|今日已领|已签到|已完成|已开通|已报名|已学习|已认证|已发布)$",
     "状态：已完成，不用再点"),
    (r"^(流量护航中|护航中|进行中|已生效|待领取)$", "状态：进行中"),
    (r"签到|打卡", "领取当日奖励"),
    (r"领取|领奖|收下|待领取", "领取奖励"),
    (r"去完成|立即完成|去处理|去查看|去设置|去开通|去看看|立即参与|马上去", "跳到对应页面处理"),
    (r"开通|报名|申请|加入", "提交开通/报名（可能产生费用）"),
    (r"^(返回|关闭|取消|×|✕|知道了|我知道了|稍后|暂不)$", "关闭弹窗/返回上一级"),
    (r"确定|确认|提交|保存|发布", "提交生效（多半不可撤销）"),
    (r"同意|允许|授权|始终允许|仅在使用中允许", "授权（改变后续权限）"),
    (r"刷新|重新加载|重试", "重新拉取数据"),
    (r"分享|转发|邀请", "调起分享面板"),
    (r"复制|粘贴", "写剪贴板"),
    (r"删除|移除|清空|注销|解绑|退出登录", "删除/解绑（不可逆）"),
    (r"支付|付款|购买|结算|下单|充值|提现", "资金操作"),
    (r"^(首页|聊天|订单|成长|我的|工作台|商品|数据|营销|消息)$", "切换底部 tab"),
    (r"下一[步页]|上一[步页]|更多|全部|展开", "翻页/展开更多"),
)


def _zone(cx: int, cy: int, screen) -> str:
    """这块区域在屏幕的哪个方位。控件树给坐标不给语义，截图给语义不给坐标，方位是
    把两者对上的桥梁——「顶部右侧那个搜索图标」能在编号表里直接查到行，不必读图上数字。"""
    w, h = (screen or (0, 0))
    if not w or not h:
        return ""
    ry, rx = cy / h, cx / w
    vs = ("顶部" if ry < 0.18 else "上部" if ry < 0.35 else
          "中部" if ry < 0.65 else "下部" if ry < 0.85 else "底部")
    hs = "左" if rx < 0.33 else "中" if rx < 0.67 else "右"
    return vs + hs


def _effect(label: str, n: dict, screen: tuple[int, int]) -> str:
    """这个元素点下去会发生什么。属性确定，词表推断加 ≈ 前缀。"""
    t = (label or "").strip()
    tags = []
    if n.get("editable"):
        tags.append("可输入文字")
    if n.get("checkable"):
        tags.append("取消勾选" if n.get("checked") else "勾选")
    if not n.get("enabled", True):
        tags.append("⚠已禁用（点了没反应）")
    # 长句是卡片/说明文字，不是按钮：对它做动作词匹配会误报（「发布商品越多，
    # 爆单机会越多」会被判成「提交生效」）。只在短标签上认动作词。
    if len(t) <= 8 and not re.search(r"[，。；：！？、]", t):
        for pat, eff in _EFFECT_RULES:
            if re.search(pat, t):
                tags.append("≈" + eff)
                break
    elif t:
        tags.append("≈卡片/说明文字，多半进详情")
    if not tags:
        h = screen[1] if screen else 0
        if not t:
            tags.append("无文字可点区域")
        elif h and n["cy"] > h * 0.88 and len(t) <= 6:
            tags.append("≈切换底部 tab")
        elif n.get("scrollable"):
            tags.append("可滚动")
        else:
            tags.append("≈点击（效果未知）")
    return " · ".join(tags)


def click_regions(nodes: list[dict], screen=None) -> tuple[list[dict], list[dict]]:
    """聚合出「所有能点的地方」：每个可点区域一条，带标签、坐标、预期效果。

    只认节点自身 clickable 会漏掉一大片：真实 App 的按钮/卡片是父容器 clickable、
    里面的文字子节点 clickable=false。于是「签到」这种大按钮在可点列表里整片消失
    ——文字进了文字列表，容器没文字被丢弃。做法：把每个有文字的节点归到包含它的
    最内层可点元素名下，以那个元素为单位输出。

    无文字的可点元素保留（图标按钮就是这种），但它内部已有别的可点元素时跳过
    ——否则每层容器都出一条，全是噪音。
    """
    if screen is None:
        screen = screen_size()
    clk = [(i, n) for i, n in enumerate(nodes) if n.get("clickable")]
    if not clk:
        return [], nodes

    def area(b):
        return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

    def inside(o, i):
        return o[0] <= i[0] and o[1] <= i[1] and o[2] >= i[2] and o[3] >= i[3]

    owner_of, free = {}, []
    for n in nodes:
        lab = (n["text"] or n["desc"]).strip()
        if not lab:
            continue
        best, best_a = None, None
        for j, c in clk:
            if not inside(c["b"], n["b"]):
                continue
            a = area(c["b"])
            if best_a is None or a < best_a:
                best, best_a = j, a
        if best is None:
            # 严格包含失败时退一步：中心点落在谁里面（列表项会被父容器裁剪）
            for j, c in clk:
                b = c["b"]
                if b[0] <= n["cx"] <= b[2] and b[1] <= n["cy"] <= b[3]:
                    a = area(b)
                    if best_a is None or a < best_a:
                        best, best_a = j, a
        if best is None:
            free.append(n)
        else:
            owner_of.setdefault(best, []).append(lab)

    screen_area = (screen[0] * screen[1]) if screen else 0
    out = []
    for j, c in clk:
        labs = owner_of.get(j) or []
        own = (c["text"] or c["desc"]).strip()
        if own and own not in labs:
            labs = [own] + labs
        if not labs:
            # 内部还有别的可点元素时跳过：子元素已经代表了这块区域
            if any(k != j and inside(c["b"], kk["b"]) for k, kk in clk):
                continue
        # 全屏根容器把整页都吞了：降级成文字，不当作一个「能点的地方」
        if screen_area and area(c["b"]) >= screen_area * 0.9 and len(labs) > 6:
            free.extend(n for n in nodes
                        if (n["text"] or n["desc"]).strip() in labs)
            continue
        label = " / ".join(dict.fromkeys(labs))[:80]
        out.append({"t": label, "c": [c["cx"], c["cy"]], "b": c["b"],
                    "cls": (c["cls"] or "").split(".")[-1],
                    "id": (c["rid"] or "").split("/")[-1],
                    "own": True, "eff": _effect(label, c, screen)})
    return out, free


def snapshot() -> dict | None:
    """现场 dump 一次，生成与实时层同构的快照。实时层守护也调这里，只有一份实现。
    ts 取 dump 发起时刻而不是结束时刻：层次结构反映的是发起那一刻的屏，取结束
    时刻等于给 0.3 秒前的旧屏发新鲜证明。"""
    t0 = round(time.time(), 3)
    win = norm_win(cur_window())
    nodes = nodes_now()
    if not nodes:
        return None
    w, h = screen_size()
    regs, free = click_regions(nodes, (w, h))
    clickable, texts, seen = [], [], set()
    for it in regs:
        key = (it["t"], it["c"][0], it["c"][1])
        if key in seen:
            continue
        seen.add(key)
        clickable.append(it)
    for n in free:
        lab = (n["text"] or n["desc"]).strip()
        if not lab:
            continue
        key = (lab, n["cx"], n["cy"])
        if key in seen:
            continue
        seen.add(key)
        texts.append({"t": lab, "c": [n["cx"], n["cy"]],
                      "b": n.get("b") or [0, 0, 0, 0],
                      "cls": (n["cls"] or "").split(".")[-1],
                      "id": (n["rid"] or "").split("/")[-1]})
    return {"ts": t0, "v": SNAP_V, "win": win, "screen": [w, h],
            "clickable": clickable[:80], "texts": texts[:60]}


def write_snap(data: dict) -> None:
    """写缓存。旧屏不许盖新屏：守护进程和调用方会并发写，守护那发可能是 0.3 秒前
    发起的 dump，期间界面已经变了——ts 取 dump 发起时刻（见 snapshot）+ 这里比 ts，
    两道才拦得住「旧屏带新时间戳」这种最骗人的缓存。"""
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


def snap_fp(s: dict | None = None) -> str:
    """界面指纹：快照内容的哈希。静止页面稳定，是判断「界面变了没有」最便宜的探针。"""
    s = s if s is not None else fresh_snap(FRESH["fp"])
    if not s:
        return ""
    body = "|".join(f"{i['t']}@{i['c'][0]},{i['c'][1]}" for i in
                    (s.get("clickable") or []) + (s.get("texts") or []))
    return hashlib.md5(((s.get("win") or "") + body).encode()).hexdigest()


def settle(prev: str, timeout: float = 2.5, quiet: float = 1.2) -> str:
    """动作后轮询到界面真的变了再返回，替代固定 sleep。
    存在的理由：固定 sleep 两头不讨好——界面 0.3s 就绪时白等 1.5s；界面 2s 才
    加载完时 1.5s 后缓存里还是上一页，而旧屏看起来完全正常、不报错。
    坑一：dump 在发起那一刻抓屏，界面还没切完就拿到旧结构。指纹比对天然挡掉了它——
    没变就不写缓存，所以「age 0.0s 的旧屏」这种最骗人的缓存写不进去，可以放心立刻
    开轮询、不用先盲等（实测 0.35s 盲等不够：tap 我 抓到新页、tap 首页 抓到个人页）。
    坑二：只认「连续两发指纹一致」才写，避免把过渡动画的半成品写进缓存。
    代价：每轮 snapshot 约 0.35s（agent dump 0.2s + dumpsys window 0.13s），
    所以界面真变了通常 0.7~1.4s 返回，没变则等满 timeout 返回 prev（不写缓存，
    让下一条 see 如实显示缓存年龄）。

    原地动作快判（2026-09-13 实测后加）：点赞/勾选这类动作窗口不变、内容也可能不变，
    旧逻辑一律等满 timeout。现在拿快照里的 win 字段当免费窗口探针（不额外发 dumpsys），
    窗口始终没动 + 过了 quiet 秒内容仍与 prev 一致 → 直接返回 prev。实测每步 2.4s → 0.9s，
    3 步点击从 7.2s 降到 3.8s。轮询间隔 0.15 → 0.05，跳转场景也少等 0.1~0.3s。
    窗口一变（moved）就永不快判，避免同 Activity 内切页被误判成「没变化」。"""
    t0, seen, changed, last = time.time(), "", prev, None
    w0, moved = "", False
    while time.time() - t0 < timeout:
        s = snapshot()
        if s:
            w = norm_win(s.get("win") or "")
            if w and not w0:
                w0 = w
            elif w and w != w0:
                moved = True
            h = snap_fp(s)
            if h and h != prev:
                if changed == prev:
                    changed = h   # 首次看到变化就认下：页面跳转后图片/列表持续加载，
                                  # 指纹可能永远不收敛，「没稳定」不等于「没生效」
                if h == seen:
                    write_snap(s)
                    return h
                seen, last = h, s
            elif not moved and time.time() - t0 >= quiet:
                return prev
        time.sleep(0.05)
    if last:
        write_snap(last)  # 指纹不收敛（列表/图片还在加载）也要落盘：不写的话缓存里一直
                          # 是上一屏，see 会拿保质期内的旧屏如实汇报，看着完全正常
    return changed


def refresh_snap() -> str:
    """强制现场刷新实时层缓存（用户手动操作过手机、或守护进程刚好没采到时用）。"""
    s = snapshot()
    if not s:
        return "✗ dump 失败（播放视频时 uiautomator 会卡 idle）"
    write_snap(s)
    clicks, texts = snap_pairs(s)
    return f"✓ 已刷新｜{s['win']}｜{len(clicks)} 可点 / {len(texts)} 文字"


def norm_win(raw: str) -> str:
    """dumpsys 给的是 Window{4439a80 u0 com.x.x/...}，只留包名/Activity。"""
    m = re.search(r"([A-Za-z][\w.]*)/[\w.$]+", raw)
    return m.group(0) if m else raw.strip()


def fresh_snap(max_age: float = FRESH["look"], check_win: bool = False) -> dict | None:
    """实时层缓存的 UI 快照。现场 dump 要 0.8~11 秒（播放视频时 uiautomator
    会卡 idle 直接失败），缓存新鲜就别再问手机一遍。

    check_win=True 时顺手核对缓存窗口是不是当前前台窗口（dumpsys，132ms）：冷启/切
    app 时 settle 可能整段 dump 失败、没写成缓存，于是「上一个 app 的 8 秒内旧屏」
    会被当新鲜缓存用——实测 3 次冷启命中 1 次，see 报出来的是别的 app 的页面。
    多花 132ms 换「报的就是眼前这屏」，点按和看屏都开。
    """
    try:
        s = json.loads(SNAP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if s.get("v") != SNAP_V:
        return None      # 旧格式快照：字段对不上，当过期处理
    if time.time() - s.get("ts", 0) > max_age:
        return None
    if check_win:
        pkg = norm_win(cur_window()).split("/")[0]
        if "." in pkg and pkg not in (s.get("win") or ""):
            return None
    return s


def snap_pairs(s: dict) -> tuple[list[str], list[str]]:
    """把快照拆成（可点标签带坐标和效果, 纯文字）。"""
    clicks = []
    for i in (s.get("clickable") or []):
        if not i.get("t"):
            continue
        eff = i.get("eff") or ""
        clicks.append(f"「{i['t']}」@({i['c'][0]},{i['c'][1]})"
                      + (f" [{eff}]" if eff else ""))
    texts = [i["t"] for i in (s.get("texts") or []) if i["t"]]
    return clicks, texts


def find_hits(kw: str, max_age: float = FRESH["look"]) -> list[str]:
    """按关键词找控件，优先读实时层缓存。现场 dump 动辄好几秒，能省就省。"""
    s = fresh_snap(max_age)
    if s:
        out = []
        for tag, items in (("可点", s.get("clickable") or []),
                           ("文本", s.get("texts") or [])):
            for i in items:
                t = (i.get("t") or "").strip()
                if t and kw.lower() in t.lower():
                    c = i.get("c") or (0, 0)
                    out.append((kw_rank(kw, t), f"({c[0]},{c[1]}) {tag} :: {t[:60]}"))
        if out:
            out.sort(key=lambda x: x[0])
            return [s for _, s in out[:15]]
    hits = match(nodes_now(), kw)
    return [f"({n['cx']},{n['cy']}) {'可点' if n['clickable'] else '不可点'} "
            f"{n['cls'].split('.')[-1]} :: {(n['text'] or n['desc'] or n['rid'])[:60]}"
            for n in hits[:15]]


_CLS_HINT = {
    "ImageView": "图标按钮", "ImageButton": "图标按钮", "Button": "按钮",
    "TextView": "文字/标签", "EditText": "输入框", "CheckBox": "勾选",
    "Switch": "开关", "RecyclerView": "列表项", "ListView": "列表项",
    "ViewPager": "可横向翻页", "ScrollView": "可滚动", "WebView": "网页内容",
    "TabLayout": "标签页", "View": "可点区域", "WebView": "网页内容",
}
_CLS_FUZZ = (("image", "图标按钮"), ("button", "按钮"), ("text", "文字/标签"),
             ("edit", "输入框"), ("list", "列表项"), ("scroll", "可滚动"),
             ("switch", "开关"), ("check", "勾选"), ("tab", "标签页"))


def cls_hint(cls: str) -> str:
    """View Hierarchy 里没有文字，只能按类名猜这块区域是干什么的——是推断不是事实。"""
    if cls in _CLS_HINT:
        return _CLS_HINT[cls]
    low = cls.lower()
    for k, v in _CLS_FUZZ:
        if k in low:
            return v
    return "?"


_RESID = {}


def _resid_name(pkg: str, rid: str) -> str:
    """hex 资源 id -> 'id/plus_icon'。表由 tools/resid_map.py 从 APK 的 resources.arsc 生成。

    不复用 viewtree.res_name：热重载只清技能加载期导入的模块，函数内延迟 import
    的 viewtree 会一直留在 sys.modules 里不刷新，改它必须重启进程才生效。
    """
    if not rid:
        return ""
    if pkg not in _RESID:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resid", f"{pkg}.json")
        try:
            with open(p, encoding="utf-8") as f:
                _RESID[pkg] = json.load(f)
        except Exception:
            _RESID[pkg] = {}
    return _RESID[pkg].get(rid.lower().lstrip("0x"), "")


def viewtree_nodes(pkg: str = ""):
    """viewtree 通道的控件表 —— 微信这类双向屏蔽无障碍的应用只有这条路。
    返回 (窗口, 节点表)；节点表为 None 表示这段 View Hierarchy 没读到。
    坐标已做屏外平移校正、已按叶子去重。verify 定位区域也走这里。"""
    try:
        import viewtree as V
    except Exception:
        return "", None
    win = norm_win(cur_window())
    pkg = pkg or win.split("/")[0]
    # dumpsys 会把后台 Activity 也列出来，只按包名匹配会取到主页那一段：
    # NewPageActivity 叠在 MainFrameTabActivity 上时，see 报的是主页元素，照着点必错
    act = win.split("/")[-1].lstrip(".") if "/" in win else ""
    try:
        raw = V.raw_top(pkg)
        lines = V.section(raw, act) if act else []
        if not lines:
            lines = V.section(raw, pkg)
    except Exception:
        return win, None
    if not lines:
        return win, None
    nodes = V.parse(lines)
    w, h = screen_size()

    def alive(n):
        return (n["vis"] == "V" and (n["click"] or n["long"])
                and n["w"] > 0 and n["h"] > 0)

    def hits(dx):
        c = 0
        for n in nodes:
            if not alive(n):
                continue
            x1, y1, x2, y2 = n["b"]
            if 0 <= (x1 + x2) // 2 + dx < w and 0 <= (y1 + y2) // 2 < h:
                c += 1
        return c

    # ViewPager 把非当前页摆在虚拟 x 偏移上（微信发现页在 2160，拼多多 tab 页在 -3240）。
    # 判据看顶部栏：顶部栏也在屏外，说明整页带偏移，才做平移；否则老老实实用原坐标——
    # 课程详情页叠在主页上时盲目平移，会把主页那几页的元素拉进屏幕，报出一屏点不对的东西
    tops = [n for n in nodes if alive(n) and n["b"][1] < 300]
    off_top = bool(tops) and all(n["b"][2] <= 0 or n["b"][0] >= w for n in tops)
    dx = max([k * w for k in range(-4, 5)], key=hits) if off_top else 0
    regs = []
    for n in nodes:
        if not alive(n):
            continue
        x1, y1, x2, y2 = n["b"]
        x1, x2 = x1 + dx, x2 + dx
        if x2 <= 0 or x1 >= w or y2 <= 0 or y1 >= h:
            continue          # 屏外的不列：列了也点不到
        regs.append(dict(n, b=(x1, y1, x2, y2)))
    # 容器套容器：只留叶子，否则一屏几十个父容器会把真正的按钮淹掉
    leaf = []
    for n in regs:
        a1, b1, a2, b2 = n["b"]
        inner = any(m is not n and m["b"][0] >= a1 and m["b"][1] >= b1
                    and m["b"][2] <= a2 and m["b"][3] <= b2
                    and (m["w"] < n["w"] or m["h"] < n["h"]) for m in regs)
        if not inner:
            leaf.append(n)
    regs = leaf or regs
    uniq, seen = [], set()
    for n in sorted(regs, key=lambda n: (n["b"][1], n["b"][0])):
        key = (V.short(n["cls"]), n["b"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(n)
    return win, uniq


def see_viewtree(pkg: str = "") -> str:
    """解析 dumpsys activity top 的 View Hierarchy。
    微信这类双向屏蔽无障碍的应用只有这条通道。代价：View Hierarchy 只有结构
    （类名/坐标/可点标志/资源 id），没有文字，效果只能按类名推断，一律带 ≈。"""
    import viewtree as V
    win, regs = viewtree_nodes(pkg)
    if regs is None:
        return ""
    pkg = pkg or win.split("/")[0]
    out = [f"窗口 {win} | viewtree 通道 | {len(regs)} 可点 / 0 文字",
           "可点（View Hierarchy 不含文字，效果按类名推断，≈ = 推断）：",
           "提示：这个 app 控件树不给文字，tap/tap_text 按文字找会全落空——要按文字点，先 annotate 拿编号→坐标表再 xy。"]
    if not regs:
        out.append("  （可点元素都在屏外或属于自绘内容，viewtree 也读不到）")
    for n in regs[:40]:
        x1, y1, x2, y2 = n["b"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        cls = V.short(n["cls"])
        eff = cls_hint(cls)
        if n["long"]:
            eff = eff + "，可长按"
        nm = _resid_name(pkg, n["id"])
        out.append(f"  {cls} @({cx},{cy}) {n['w']}x{n['h']} [≈ {eff}] "
                   f"{n['id']}" + (f" ← {nm}" if nm else ""))
    if len(regs) > 40:
        out.append(f"  …还有 {len(regs) - 40} 个")
    return "\n".join(out)


def do_see(max_age: float = FRESH["look"]) -> str:
    """一次拿全当前页：窗口 + 所有能点的地方（带点下去的效果）+ 全部文字。
    存在的理由：find→page→cur 三次调用就是三次 LLM 往返，每趟好几秒。
    [] 里是预计效果：属性直接读出来的是确定的，词表推断的带 ≈。"""
    s = fresh_snap(max_age, check_win=True)
    if s:
        win = s.get("win") or "?"
        src = f"缓存 {time.time() - s['ts']:.1f}s 前"
        regs = s.get("clickable") or []
        texts = [i["t"] for i in (s.get("texts") or []) if i["t"]]
    else:
        nodes = nodes_now()
        if not nodes:
            vt = see_viewtree()
            if vt:
                return vt
            return "✗ dump 失败（没抓到控件，重试一次）"
        win, src = norm_win(cur_window()), "现场 dump"
        regs, free = click_regions(nodes, screen_size())
        texts, seen = [], set()
        for n in free:
            lab = (n["text"] or n["desc"]).strip()
            if lab and lab not in seen:
                seen.add(lab)
                texts.append(lab)
    # 微信这类应用：uiautomator 拿到的只有状态栏和悬浮导航条（实测 5 个可点、11 条文字
    # 全是系统 UI）；拼多多的课程详情页同理。「有节点但没料」比「没节点」更骗人——
    # 可点数和文字数一起兜底。
    if len(regs) < 8 or len(texts) < 15:
        vt = see_viewtree()
        if vt:
            return vt + (f"\n（uiautomator 只给了 {len(regs)} 个可点，全是系统 UI；"
                         f"以上走 viewtree 通道，坐标已验证可用）")
    named = sorted([r for r in regs if r.get("t")],
                   key=lambda r: (r["c"][1], r["c"][0]))
    anon = sorted([r for r in regs if not r.get("t")],
                  key=lambda r: (r["c"][1], r["c"][0]))
    lines = [f"窗口 {win} | {src} | {len(regs)} 可点（{len(named)} 带文字 / "
             f"{len(anon)} 纯图标）/ {len(texts)} 文字"]
    lines.append("可点（每条 = 一块能点的区域，[] 里是点下去预计会怎样）：")
    for i in named[:40]:
        cls = f" {i['cls']}" if i.get("cls") else ""
        lines.append(f"  「{i['t']}」@({i['c'][0]},{i['c'][1]}){cls} "
                     f"[{i.get('eff') or '?'}]")
    if len(named) > 40:
        lines.append(f"  …还有 {len(named) - 40} 个带文字的（find 精确找）")
    if anon:
        lines.append("无文字可点区域（图标按钮，按坐标点）："
                     + " ".join(f"@({r['c'][0]},{r['c'][1]})" for r in anon[:24]))
        if len(anon) > 24:
            lines.append(f"  …还有 {len(anon) - 24} 个")
    if not regs:
        lines.append("  （无）")
    lines.append("文字 " + (" / ".join(texts[:60]) or "（无）"))
    return "\n".join(lines)


def do_annotate(limit: int = 50, max_age: float = FRESH["look"]) -> str:
    """把每个可点区域编号画到截图上，返回「编号 → 坐标」表。

    分工：眼睛只回答「这一块是干什么的」，坐标由 View Hierarchy 精确给出——
    空间回归是视觉模型最不擅长的活（Lacuna/SoM 论文实测：普通 LMM 直接输出
    click(x,y) 会 hallucinate）。一次调用 = 一张标注图 + 一张带方位的编号表，
    之后所有点击只引用编号或方位，不再回头问屏幕。
    """
    s = fresh_snap(max_age, check_win=True)
    if s and (s.get("clickable") or []):
        regs, win = s["clickable"], s.get("win") or "?"
    else:
        nodes = nodes_now()
        if not nodes:
            return "✗ dump 失败（没抓到控件，重试一次）"
        regs, _free = click_regions(nodes, screen_size())
        win = norm_win(cur_window())
    if not regs:
        return "✗ 这屏没有可点区域"
    regs = sorted(regs, key=lambda r: (r["c"][1], r["c"][0]))[:limit]
    screen = screen_size()
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = SHOT_DIR / f"annot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    p.write_bytes(shb("exec-out", "screencap", "-p", timeout=40))
    prune_shots()
    try:
        from PIL import Image, ImageDraw, ImageFont
        im = Image.open(p).convert("RGB")
        d = ImageDraw.Draw(im)
        sc = max(im.width, im.height) / 1400.0
        fs = max(26, int(46 * sc))
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", fs)
        except Exception:
            font = ImageFont.load_default()
        pad = int(7 * sc)
        for i, r in enumerate(regs, 1):
            b = r["b"]
            d.rectangle(b, outline=(255, 210, 0), width=max(2, int(3 * sc)))
            t = str(i)
            tb = d.textbbox((0, 0), t, font=font)
            w, h = tb[2] - tb[0], tb[3] - tb[1]
            x, y = b[0], max(0, b[1] - h - 2 * pad)
            d.rectangle([x, y, x + w + 2 * pad, y + h + 2 * pad],
                        fill=(255, 210, 0), outline=(0, 0, 0),
                        width=max(2, int(2 * sc)))
            d.text((x + pad, y + pad), t, fill=(0, 0, 0), font=font)
        im.save(p)
        size = f"{im.width}x{im.height}"
    except Exception as e:
        return f"✗ 画框失败：{e}（截图已存 {p}）"
    lines = [f"[[IMG:{p}]]",
             f"标注图 {p} | {size} | 窗口 {win} | {len(regs)} 个编号区域",
             "编号 @(x,y) 方位 标签 [预计效果]（方位 = 它在屏幕的哪个位置，"
             "用来把截图里看到的东西对上这一行；坐标可直接 xy 点）："]
    for i, r in enumerate(regs, 1):
        z = _zone(r["c"][0], r["c"][1], screen)
        lines.append(f" {i:>2} @({r['c'][0]},{r['c'][1]}) {z} "
                     f"{r.get('t') or '-'} [{r.get('eff') or '?'}]")
    return "\n".join(lines)


def do_probe(kw: str) -> str:
    """点一下，然后如实报告界面到底变了什么——效果的实测，不是推断。"""
    b = snapshot()
    if not b:
        return "✗ 起始 dump 失败"
    write_snap(b)
    r = do_tap(kw)
    if "✓" not in r:
        return r
    time.sleep(1.6)
    a = snapshot()
    if not a:
        return r + "\n✗ 点后 dump 失败（界面可能正在加载）"
    write_snap(a)

    def labs(x):
        return {i["t"] for i in (x.get("texts") or []) + (x.get("clickable") or [])
                if i.get("t")}
    bt, at = labs(b), labs(a)
    added = [t for t in at - bt][:8]
    gone = [t for t in bt - at][:8]
    win = (f"窗口变了 → {a.get('win')}" if b.get("win") != a.get("win")
           else "窗口没变")
    return (f"{r}\n{win}\n新增：{' / '.join(added) or '（无）'}\n"
            f"消失：{' / '.join(gone) or '（无）'}")


def do_page() -> str:
    """一次把当前页的文字全抽出来。text 和 content-desc 都要抽：不少 App
    把列表项标题只写在 content-desc 里，光看 text 会误以为页面是空的。"""
    s = fresh_snap(FRESH["look"])
    if s:
        clicks, texts = snap_pairs(s)
        out, seen = [], set()
        for v in texts + [c.split("」")[0][1:] for c in clicks]:
            v = v.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        if out:
            return "\n".join(out)
    nodes = nodes_now()
    seen, out = set(), []
    for n in nodes:
        for v in (n["text"], n["desc"]):
            v = v.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return "\n".join(out) if out else "✗ 没抓到任何文字（多半是 dump 失败，重试一次）"


def do_screen() -> str:
    _, size = sh("shell", "wm", "size")
    return f"{cur_window()}\n{size}"


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 0
    cmd, rest = a[0].lower(), a[1:]
    if cmd == "cur":
        print(cur_window())
    elif cmd == "screen":
        print(do_screen())
    elif cmd == "find":
        hits = find_hits(rest[0])
        if not hits:
            print(f"✗ 没找到「{rest[0]}」")
            return 1
        print("\n".join(hits))
    elif cmd == "see":
        print(do_see())
    elif cmd == "probe":
        print(do_probe(rest[0]))
    elif cmd == "page":
        print(do_page())
    elif cmd == "tap":
        print(do_tap(rest[0]))
    elif cmd == "tap-all":
        print(do_tap(rest[0], all_hits=True))
    elif cmd == "wait":
        print(do_wait(rest[0], float(rest[1]) if len(rest) > 1 else 10.0))
    elif cmd == "seq":
        print(do_seq(rest[0]))
    elif cmd == "batch":
        print(do_batch(" ".join(rest)))
    elif cmd == "xy":
        print(tap_xy(int(rest[0]), int(rest[1])))
    elif cmd == "swipe":
        d = rest[4] if len(rest) > 4 else "300"
        sh("shell", "input", "swipe", *rest[:4], d)
        print(f"✓ 滑动 {','.join(rest[:4])} {d}ms")
    elif cmd == "key":
        sh("shell", "input", "keyevent", rest[0])
        print(f"✓ 按键 {rest[0]}")
    elif cmd == "text":
        sh("shell", "input", "text", rest[0])
        print(f"✓ 输入 {rest[0]}")
    elif cmd == "ctext":
        print(do_ctext(" ".join(rest)))
    elif cmd == "ime":
        print(do_ime(rest[0] if rest else "adb"))
    elif cmd == "shot":
        print(do_shot(rest[0] if rest else None))
    elif cmd == "sms":
        print(do_sms(int(rest[0]) if rest else 5))
    elif cmd == "code":
        print(do_code())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
