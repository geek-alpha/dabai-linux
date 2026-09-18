#!/usr/bin/env python3
"""Battle Phoenix Tech — public read-only demo API.

暴露的能力全部「不碰文件系统、不执行外部命令」：
  GET  /api/health        服务状态
  GET  /api/skills        技能包清单（实时读 skills/*/skill.json）
  GET  /api/spec/<name>   单个技能包完整 schema
  GET  /api/system        本机真实硬件状态（脱敏）
  POST /api/analyze       Python 代码结构分析（AST，纯内存）

安全边界（这是公网暴露面，每条都是刻意的）：
  - 只 bind 127.0.0.1，经 cloudflared 出网，不直接对外
  - /api/analyze 只做 ast.parse，绝不 exec/eval/import/落盘
  - 请求体 ≤ 16KB，代码 ≤ 8000 字符
  - 每 IP 滑动窗口限流 + 全局串行锁（905MB 内存的板子不接并发洪水）
  - CORS 白名单；异常一律转 400，不回吐 traceback
"""

import ast
import json
import os
import time
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKILLS_DIR = "/home/wxf/dabai/skills"
HOST, PORT = "127.0.0.1", 8089
STARTED = time.time()

MAX_BODY = 16 * 1024
MAX_CODE = 8000
RATE_WINDOW = 60.0
RATE_MAX = 40
ALLOWED_ORIGINS = {
    "https://battlephoenix.tech",
    "https://www.battlephoenix.tech",
    "https://home.battlephoenix.tech",
    "http://127.0.0.1:8088",
    "http://localhost:8088",
}

_lock = threading.Lock()
_hits = {}
_analyze_lock = threading.Lock()


def _rate_ok(ip):
    now = time.time()
    with _lock:
        q = _hits.setdefault(ip, deque())
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        if len(_hits) > 2000:
            for k in [k for k, v in _hits.items() if not v][:500]:
                _hits.pop(k, None)
        return True


def _load_skills():
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for name in sorted(os.listdir(SKILLS_DIR)):
        p = os.path.join(SKILLS_DIR, name, "skill.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        tools = [t for t in (d.get("tools") or []) if isinstance(t, dict)]
        names = [n for n in ((t.get("function") or {}).get("name") for t in tools) if n]
        out.append({
            "name": d.get("name") or name,
            "title": d.get("title") or name,
            "version": d.get("version") or "",
            "description": d.get("description") or "",
            "author": d.get("author") or "",
            "disclosure": d.get("disclosure") or "always",
            "tool_count": len(names),
            "tools": names,
        })
    return out


def _read_first(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return default


def _system_snapshot():
    """只读 /proc 与固定 sysfs 路径，输出脱敏后的整机状态。"""
    snap = {"cpu_temp_c": None, "load": None, "mem_total_mb": None,
            "mem_avail_mb": None, "uptime_s": None, "disk_free_pct": None,
            "cpu_mhz": None, "throttled": None}

    raw = _read_first("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.isdigit():
        snap["cpu_temp_c"] = round(int(raw) / 1000.0, 1)

    try:
        with open("/proc/loadavg") as fh:
            snap["load"] = [float(x) for x in fh.read().split()[:3]]
    except Exception:
        pass

    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    snap["mem_total_mb"] = round(int(line.split()[1]) / 1024)
                elif line.startswith("MemAvailable:"):
                    snap["mem_avail_mb"] = round(int(line.split()[1]) / 1024)
    except Exception:
        pass

    try:
        with open("/proc/uptime") as fh:
            snap["uptime_s"] = int(float(fh.read().split()[0]))
    except Exception:
        pass

    try:
        st = os.statvfs("/")
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
        snap["disk_free_pct"] = round(100.0 * free / total, 1)
    except Exception:
        pass

    for line in (_read_first("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") or "").splitlines():
        if line.strip().isdigit():
            snap["cpu_mhz"] = round(int(line.strip()) / 1000)
            break

    thr = _read_first("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if thr and thr.strip():
        try:
            snap["throttled"] = hex(int(thr.strip(), 16))
        except Exception:
            snap["throttled"] = thr.strip()

    snap["ok"] = snap["cpu_temp_c"] is not None or snap["mem_total_mb"] is not None
    return snap


def _complexity(node):
    score = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                          ast.With, ast.AsyncWith, ast.IfExp, ast.BoolOp)):
            score += 1
    return score


def _depth(node, cur=0):
    best = cur
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If,
                              ast.For, ast.While, ast.With, ast.Try)):
            best = max(best, _depth(child, cur + 1))
        else:
            best = max(best, _depth(child, cur))
    return best


def analyze_code(src):
    """纯内存 AST 解析。不 exec、不 import、不落盘。"""
    if len(src) > MAX_CODE:
        raise ValueError("代码超长（上限 %d 字符）" % MAX_CODE)
    try:
        tree = ast.parse(src)
    except RecursionError:
        raise ValueError("嵌套过深，AST 解析中止")
    except SyntaxError as e:
        raise ValueError("语法错误：第 %s 行 %s" % (e.lineno, e.msg))

    lines = src.count("\n") + 1
    symbols, imports = [], []

    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                mod = a.name if isinstance(n, ast.Import) else (n.module or "")
                if mod and mod not in imports:
                    imports.append(mod)

    def walk(body, parent=""):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(n, "end_lineno", n.lineno)
                args = [a.arg for a in n.args.args] + [a.arg for a in n.args.kwonlyargs]
                if n.args.vararg:
                    args.append("*" + n.args.vararg.arg)
                if n.args.kwarg:
                    args.append("**" + n.args.kwarg.arg)
                symbols.append({
                    "kind": "async function" if isinstance(n, ast.AsyncFunctionDef) else "function",
                    "name": (parent + "." if parent else "") + n.name,
                    "line": n.lineno, "lines": end - n.lineno + 1,
                    "args": args, "complexity": _complexity(n),
                    "doc": bool(ast.get_docstring(n)),
                })
                walk(n.body, (parent + "." if parent else "") + n.name)
            elif isinstance(n, ast.ClassDef):
                end = getattr(n, "end_lineno", n.lineno)
                bases = [b.id for b in n.bases if isinstance(b, ast.Name)]
                symbols.append({
                    "kind": "class", "name": (parent + "." if parent else "") + n.name,
                    "line": n.lineno, "lines": end - n.lineno + 1,
                    "args": bases, "complexity": _complexity(n),
                    "doc": bool(ast.get_docstring(n)),
                })
                walk(n.body, (parent + "." if parent else "") + n.name)

    walk(tree.body)
    hot = sorted([s for s in symbols if s["kind"] != "class"],
                 key=lambda s: -s["complexity"])[:5]
    return {
        "ok": True, "lines": lines, "bytes": len(src.encode()),
        "max_nesting": _depth(tree), "imports": imports,
        "counts": {
            "classes": sum(1 for s in symbols if s["kind"] == "class"),
            "functions": sum(1 for s in symbols if s["kind"] == "function"),
            "async_functions": sum(1 for s in symbols if s["kind"] == "async function"),
            "with_docstring": sum(1 for s in symbols if s["doc"]),
        },
        "symbols": symbols, "hotspots": hot,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "bp-demo/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def _guard(self):
        if not _rate_ok(self._ip()):
            self._send(429, {"ok": False, "error": "请求太频繁，请稍后再试"})
            return False
        return True

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._guard():
            return
        try:
            if path == "/api/health":
                self._send(200, {"ok": True, "service": "bp-demo-api", "version": "1.0",
                                 "uptime_s": int(time.time() - STARTED)})
            elif path == "/api/skills":
                skills = _load_skills()
                self._send(200, {"ok": True, "count": len(skills),
                                 "tool_count": sum(s["tool_count"] for s in skills),
                                 "skills": skills})
            elif path.startswith("/api/spec/"):
                want = path[len("/api/spec/"):]
                if not want or "/" in want or want.startswith("."):
                    self._send(400, {"ok": False, "error": "非法技能包名"})
                    return
                p = os.path.join(SKILLS_DIR, want, "skill.json")
                if not os.path.isfile(p):
                    self._send(404, {"ok": False, "error": "技能包不存在"})
                    return
                with open(p, encoding="utf-8") as fh:
                    self._send(200, {"ok": True, "spec": json.load(fh)})
            elif path == "/api/system":
                self._send(200, {"ok": True, "snapshot": _system_snapshot()})
            else:
                self._send(404, {"ok": False, "error": "未知端点",
                                 "endpoints": ["/api/health", "/api/skills",
                                               "/api/spec/<name>", "/api/system",
                                               "/api/analyze"]})
        except Exception:
            self._send(500, {"ok": False, "error": "服务内部错误"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._guard():
            return
        if path != "/api/analyze":
            self._send(404, {"ok": False, "error": "未知端点"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(413, {"ok": False, "error": "请求体为空或超过 16KB"})
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            code = data.get("code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("缺少 code 字段（字符串）")
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)[:120]})
            return
        if not _analyze_lock.acquire(blocking=False):
            self._send(429, {"ok": False, "error": "分析器忙，请稍后再试"})
            return
        try:
            self._send(200, analyze_code(code))
        except ValueError as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception:
            self._send(500, {"ok": False, "error": "分析失败"})
        finally:
            _analyze_lock.release()


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("bp-demo-api on http://%s:%d (skills: %d)" % (HOST, PORT, len(_load_skills())), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
