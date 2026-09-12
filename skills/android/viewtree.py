#!/usr/bin/env python3
"""dumpsys activity top 的 View Hierarchy 解析器 —— 微信这类屏蔽 uiautomator 的应用的第三条通道。

微信对 uiautomator 屏蔽（agent 树里只剩 1 个 0x0 空壳节点、零文字），但 `dumpsys activity top` 里
的 View Hierarchy 段照样打印完整视图树：类名、相对父容器的 bounds、visibility flags、
资源 id。坐标是相对的，必须按缩进层级累加祖先偏移才是屏幕绝对坐标。

用法:
  python3 viewtree.py                 # 取当前前台应用，打印可点元素
  python3 viewtree.py --pkg com.tencent.mm
  python3 viewtree.py --file tmp/top.txt --pkg com.tencent.mm --all
  python3 viewtree.py --pkg com.tencent.mm --text      # 附带节点类名/尺寸明细
"""
import json, re, sys, subprocess, os

_RESID = {}

LINE = re.compile(
    r"^(?P<ind>\s*)(?P<cls>[\w.$]+)\{(?P<hash>\w+)\s+"
    r"(?P<f1>\S{9})\s+(?P<f2>\S{8})\s+"
    r"(?P<b>-?\d+,-?\d+--?\d+,-?\d+)"
    r"(?:\s+#(?P<id>\S+))?"
    r"(?:\s+(?P<aid>[\w.]+/[\w.]+))?"
)
BOUNDS = re.compile(r"(-?\d+),(-?\d+)-(-?\d+),(-?\d+)")
SERIAL_CACHE = {}


def _serial():
    if "s" in SERIAL_CACHE:
        return SERIAL_CACHE["s"]
    env = os.environ.get("ADB_SERIAL", "").strip()
    if env:
        SERIAL_CACHE["s"] = env
        return env
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    devs = [l.split()[0] for l in out.splitlines()[1:] if "\tdevice" in l]
    usb = [d for d in devs if ":" not in d]
    SERIAL_CACHE["s"] = (usb or devs or [""])[0]
    return SERIAL_CACHE["s"]


def raw_top(pkg=None):
    """取 dumpsys activity top 原文。pkg 只是提示，实际整段取回。"""
    s = _serial()
    cmd = ["adb"] + (["-s", s] if s else []) + ["shell", "dumpsys", "activity", "top"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


def section(text, pkg=None):
    """截出某个包名的 ACTIVITY 段（含其 View Hierarchy）。"""
    lines = text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.startswith("  ACTIVITY ") and (pkg is None or pkg in l):
            start = i
            break
    if start is None:
        return []
    out = []
    for l in lines[start:]:
        if l.startswith("  ACTIVITY ") and out:
            break
        out.append(l)
    return out


def parse(lines):
    """解析成带绝对坐标的扁平列表。"""
    nodes = []
    stack = []  # [(indent_len, abs_x1, abs_y1)]
    in_hier = False
    for l in lines:
        if "View Hierarchy:" in l:
            in_hier = True
            stack = []
            continue
        if not in_hier:
            continue
        m = LINE.match(l)
        if not m:
            continue
        ind = len(m.group("ind"))
        b = BOUNDS.match(m.group("b"))
        if not b:
            continue
        x1, y1, x2, y2 = (int(v) for v in b.groups())
        while stack and stack[-1][0] >= ind:
            stack.pop()
        px, py = (stack[-1][1], stack[-1][2]) if stack else (0, 0)
        ax1, ay1, ax2, ay2 = px + x1, py + y1, px + x2, py + y2
        f1, f2 = m.group("f1"), m.group("f2")
        n = {
            "cls": m.group("cls"), "id": m.group("aid") or m.group("id") or "",
            "b": (ax1, ay1, ax2, ay2),
            "w": ax2 - ax1, "h": ay2 - ay1,
            "vis": f1[0], "click": f1[6] == "C", "long": f1[7] == "L",
            "sel": f2[2] == "S", "enabled": f1[2] == "E",
            "depth": ind,
        }
        nodes.append(n)
        stack.append((ind, ax1, ay1))
    return nodes


def short(cls):
    c = cls.split(".")[-1]
    c = c.split("$")[-1]
    return c


def _resid_map(pkg):
    """资源 id -> 名称（tools/resid_map.py 从 APK 的 resources.arsc 生成）。"""
    if pkg not in _RESID:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resid", f"{pkg}.json")
        try:
            _RESID[pkg] = json.load(open(p, encoding="utf-8"))
        except Exception:
            _RESID[pkg] = {}
    return _RESID[pkg]


def res_name(pkg, rid):
    """hex 资源 id -> 'id/plus_icon'；无表或查不到返回空串。"""
    rid = (rid or "").lower().lstrip("0x")
    return (_resid_map(pkg).get(rid) or "") if (pkg and rid) else ""

def show(nodes, only_clickable=True, min_size=1, screen=None, pkg=None):
    """pkg 非空时，用本地 resid 表把 hex id 翻译成资源名。"""
    scr = screen or (1080, 2412)
    rows = []
    for n in nodes:
        x1, y1, x2, y2 = n["b"]
        if n["vis"] != "V":            # 只列可见的
            continue
        if n["w"] < min_size or n["h"] < min_size:
            continue
        if only_clickable and not (n["click"] or n["long"]):
            continue
        rows.append(n)
    rows.sort(key=lambda n: (n["b"][1], n["b"][0]))
    print(f"屏幕 {scr[0]}x{scr[1]} | 可见可点 {len(rows)} 个（按 y 排序）")
    print(f"{'#':>3} {'类名':<26} {'绝对坐标':<22} {'尺寸':<11} {'中心':<14} {'标志':<8} id")
    for i, n in enumerate(rows, 1):
        x1, y1, x2, y2 = n["b"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        flags = ("C" if n["click"] else "-") + ("L" if n["long"] else "-") + ("S" if n["sel"] else "-")
        box = f"{x1},{y1}-{x2},{y2}"
        size = f"{n['w']}x{n['h']}"
        ctr = f"({cx},{cy})"
        rid = n["id"].lower().lstrip("0x")
        name = (_resid_map(pkg).get(rid) or "") if pkg and rid else ""
        print(f"{i:>3} {short(n['cls']):<26} {box:<22} {size:<11} {ctr:<14} {flags:<8} "
              f"{n['id']}" + (f"  ← {name}" if name else ""))
    return rows


def main():
    args = sys.argv[1:]
    pkg = None
    path = None
    only_click = True
    min_size = 1
    for i, a in enumerate(args):
        if a == "--pkg":
            pkg = args[i + 1]
        elif a == "--file":
            path = args[i + 1]
        elif a == "--all":
            only_click = False
        elif a == "--min":
            min_size = int(args[i + 1])
    text = open(path).read() if path else raw_top(pkg)
    lines = section(text, pkg)
    if not lines:
        print(f"没找到 {pkg or '前台应用'} 的 ACTIVITY 段（微信被切到后台时也会这样）")
        return 1
    act = next((l.strip() for l in lines if l.startswith("  ACTIVITY")), "?")
    nodes = parse(lines)
    print(f"当前: {act}")
    show(nodes, only_clickable=only_click, min_size=min_size, pkg=pkg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
