# -*- coding: utf-8 -*-
"""MCP 客户端 —— 最小实现：JSON-RPC 2.0。

两条传输：stdio（本文件 MCPServer，子进程）与 Streamable HTTP（mcp_http.py，远程 url）。

设计取向（第一性原理）：MCP 的价值是别人的现成 server，成本是 schema 全量灌上下文。
所以这里只提供「连接 / 拉清单 / 调用 / 杀进程」四件事，进程按需存活、工具清单按需拉取。

线程模型：每个 server 一个读线程把 stdout 消息塞进队列，请求方按 id 匹配。
用队列而不是 select+readline —— BufferedReader 会预读，select 说「不可读」时缓冲里
可能已经有完整响应，那会误判超时。读线程方案跨平台且没有这个坑。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "dabai", "version": "1.0.0"}
DEFAULT_TIMEOUT = 60.0
_MAX_DESC = 400

# 资源闸门阈值（详见 resource_guard）
_TEMP_LIMIT_C = 72.0
_MEM_FLOOR_MB = 180
_MAX_LIVE = 2
_HEAVY_HINTS = ("chromium", "chrome", "firefox", "webkit", "playwright")

# mcp_http 反向 import 本模块的常量，两边都要能独立被 import
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)


class MCPError(Exception):
    """MCP 连接/协议/调用错误。"""


def format_tool_result(result: dict) -> str:
    """把 tools/call 的 result 拍平成文本（stdio 与 HTTP 两条传输共用）。"""
    result = result or {}
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        return f"[工具报错] {text or '（无输出）'}"
    return text or "（工具无输出）"


class MCPServer:
    """一个 MCP server 子进程的会话（持久，直到 disconnect 或进程死亡）。"""

    def __init__(self, name: str, command: str, args=None, env=None, cwd=None):
        self.name = name
        self.command = command
        self.args = [str(a) for a in (args or [])]
        self.env_extra = dict(env or {})
        self.cwd = cwd or None
        self.info: dict = {}
        self.tools: list = []
        self._proc: "subprocess.Popen | None" = None
        self._pgid = None
        self._inbox: "queue.Queue" = queue.Queue()
        self._lock = threading.RLock()
        self._next_id = 0
        self._stderr_path = ""
        self._stderr_fh = None

    # ---------- 进程 ----------

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _resolve_command(self) -> str:
        cmd = self.command
        if os.path.sep in cmd or cmd.startswith("."):
            if os.path.isfile(cmd):
                return cmd
        found = shutil.which(cmd)
        if not found:
            raise MCPError(f"找不到命令 '{cmd}'（不在 PATH 里）。")
        return found

    def _spawn(self):
        exe = self._resolve_command()
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env_extra.items()})
        # stderr 落文件而不是管道：没人读的管道写满会死锁住 server
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.name)
        self._stderr_path = os.path.join(log_dir, f"{safe}.stderr.log")
        self._stderr_fh = open(self._stderr_path, "ab", buffering=0)
        # 自成进程组：npx 这类包装器会再 fork 出 sh/node，只杀直接子进程会留下孤儿
        # （实测 npx 起 filesystem server 残留 3 个进程）。杀的时候要杀整组。
        extra = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        try:
            self._proc = subprocess.Popen(
                [exe] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_fh,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=self.cwd, env=env, **extra,
            )
        except Exception as e:
            raise MCPError(f"启动 {self.name} 失败：{e}")
        self._pgid = self._proc.pid if os.name == "posix" else None
        _write_pidfile(self.name, self._proc.pid)
        threading.Thread(target=self._reader, name=f"mcp-{self.name}", daemon=True).start()

    def _reader(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                self._inbox.put(msg)
        except Exception:
            pass
        finally:
            self._inbox.put(None)  # 哨兵：进程结束

    def _send(self, obj: dict):
        if not self.alive():
            raise MCPError(self._death_reason())
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            raise MCPError(f"写入 {self.name} 失败：{e}")

    def _death_reason(self) -> str:
        code = self._proc.poll() if self._proc else None
        tail = self.stderr_tail()
        return (f"{self.name} 进程已退出（退出码 {code}）"
                + (f"；stderr 末尾：{tail}" if tail else ""))

    def _request(self, method: str, params=None, timeout: float = DEFAULT_TIMEOUT):
        with self._lock:  # 同一 server 串行，id 不会错配
            self._next_id += 1
            mid = self._next_id
            payload = {"jsonrpc": "2.0", "id": mid, "method": method}
            if params is not None:
                payload["params"] = params
            self._send(payload)
            deadline = time.time() + timeout
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise MCPError(f"{method} 超时（{timeout:.0f}s，server={self.name}）")
                try:
                    msg = self._inbox.get(timeout=remain)
                except queue.Empty:
                    raise MCPError(f"{method} 超时（{timeout:.0f}s，server={self.name}）")
                if msg is None:
                    raise MCPError(self._death_reason())
                if msg.get("id") != mid:
                    continue  # server 主动通知或迟到响应，丢弃
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise MCPError(f"{method} 返回错误 [{err.get('code')}] {err.get('message')}")
                return msg.get("result")

    def stderr_tail(self, lines: int = 5) -> str:
        if not self._stderr_path or not os.path.isfile(self._stderr_path):
            return ""
        try:
            with open(self._stderr_path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-lines:]
            return " / ".join(t.strip() for t in tail if t.strip())[:500]
        except Exception:
            return ""

    # ---------- 协议 ----------

    def start(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        if self.alive() and self.info:
            return self.info
        self._spawn()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.info = result or {}
        # 初始化完成通知（无 id，无响应）
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.info

    def list_tools(self, timeout: float = DEFAULT_TIMEOUT) -> list:
        if self.tools:
            return self.tools
        result = self._request("tools/list", timeout=timeout) or {}
        self.tools = list(result.get("tools") or [])
        return self.tools

    def call_tool(self, tool: str, arguments=None, timeout: float = DEFAULT_TIMEOUT) -> str:
        result = self._request("tools/call", {
            "name": tool, "arguments": arguments or {},
        }, timeout=timeout) or {}
        return format_tool_result(result)

    def _group_alive(self) -> bool:
        """进程组里还有活人吗（含被 npx fork 出去的孙进程）。"""
        if os.name != "posix" or not self._pgid:
            return self._proc is not None and self._proc.poll() is None
        try:
            os.killpg(self._pgid, 0)
            return True
        except Exception:
            return False

    def _kill_tree(self, proc):
        """杀整棵进程树：先 SIGTERM 整组，3 秒不退再 SIGKILL 整组。"""
        if os.name == "posix":
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self._pgid, sig)
                except Exception:
                    if proc.poll() is None:
                        try:
                            proc.send_signal(sig)
                        except Exception:
                            pass
                deadline = time.time() + 3
                while time.time() < deadline:
                    if not self._group_alive():
                        break
                    time.sleep(0.05)
                if not self._group_alive():
                    break
        else:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop(self):
        proc = self._proc
        self._proc = None
        _drop_pidfile(self.name)
        if proc is not None:
            self._kill_tree(proc)
        for stream in ("stdin", "stdout"):
            try:
                getattr(proc, stream).close()
            except Exception:
                pass
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None

    def describe(self) -> str:
        si = (self.info or {}).get("serverInfo") or {}
        return f"{si.get('name', self.name)} v{si.get('version', '?')}"


# ============================================================
#  全局会话池
# ============================================================

_SERVERS: dict = {}
_LOCK = threading.RLock()
_SPECS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers.json")
_RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run")


def _pidfile(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(_RUN_DIR, f"{safe}.pid")


def _write_pidfile(name: str, pid: int):
    try:
        os.makedirs(_RUN_DIR, exist_ok=True)
        with open(_pidfile(name), "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _drop_pidfile(name: str):
    try:
        os.remove(_pidfile(name))
    except Exception:
        pass


def _pgid_alive(pgid: int) -> bool:
    """进程组里还有「活人」吗——僵尸不算。

    killpg(pgid, 0) 对「只剩僵尸的组」也返回成功，那会让回收逻辑误判成没杀干净
    （白等 3 秒再补一发 SIGKILL），也会让 stale 检测把死组报成孤儿。所以直接看
    /proc/<pid>/stat 的状态字段：Z 的不算。
    """
    try:
        entries = os.listdir("/proc")
    except Exception:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
        except Exception:
            continue
        rp = data.rfind(")")  # comm 里可能有空格和括号，从最后一个 ')' 往后切
        if rp < 0:
            continue
        fields = data[rp + 2:].split()
        if len(fields) < 3:
            continue
        if fields[0] != "Z" and fields[2].isdigit() and int(fields[2]) == pgid:
            return True
    return False


def stale_servers() -> list:
    """pid 文件里有、内存里没有的进程组 —— 技能模块热重载后留下的孤儿。

    改 skills/mcp/*.py 会触发模块重载，_SERVERS 字典清空，但子进程（独立进程组）
    还活着：工具再也管不到它们，只能手工 kill，而且一直吃内存（实测 github 孤儿
    88MB+68MB）。所以 spawn 时把 pgid 落盘，随时能对账。
    """
    live = {s.name for s in running()}
    out = []
    if not os.path.isdir(_RUN_DIR):
        return out
    for fn in os.listdir(_RUN_DIR):
        if not fn.endswith(".pid"):
            continue
        name = fn[:-4]
        if name in live:
            continue
        try:
            with open(os.path.join(_RUN_DIR, fn)) as f:
                pgid = int(f.read().strip())
        except Exception:
            _drop_pidfile(name)
            continue
        if _pgid_alive(pgid):
            out.append({"name": name, "pgid": pgid})
        else:
            _drop_pidfile(name)
    return out


def reap_stale() -> list:
    """杀掉孤儿进程组，返回被清的清单。"""
    killed = []
    for item in stale_servers():
        pgid = item["pgid"]
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except Exception:
                break
            deadline = time.time() + 3
            while time.time() < deadline and _pgid_alive(pgid):
                time.sleep(0.05)
            if not _pgid_alive(pgid):
                break
        _drop_pidfile(item["name"])
        killed.append(item)
    return killed


def _load_raw() -> dict:
    if not os.path.isfile(_SPECS_PATH):
        return {}
    try:
        with open(_SPECS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def load_specs() -> dict:
    """真正的 server 配置；下划线开头的键（_comment 等）只是给人看的注释，不算 server。"""
    return {k: v for k, v in _load_raw().items() if not str(k).startswith("_")}


def save_spec(name: str, spec: dict):
    specs = _load_raw()  # 保留注释键，别把它们冲掉
    specs[name] = spec
    with open(_SPECS_PATH, "w", encoding="utf-8") as f:
        json.dump(specs, f, ensure_ascii=False, indent=2)


def _read_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None


def _read_avail_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def resource_state() -> dict:
    return {"temp_c": _read_temp_c(), "avail_mb": _read_avail_mb(),
            "live": len(running()), "temp_limit": _TEMP_LIMIT_C,
            "mem_floor_mb": _MEM_FLOOR_MB, "max_live": _MAX_LIVE}


def _fmt_state(st: dict) -> str:
    t = f"{st['temp_c']:.0f}°C" if st["temp_c"] is not None else "?°C"
    m = f"{st['avail_mb']:.0f}MB" if st["avail_mb"] is not None else "?MB"
    return f"温度 {t} / 可用内存 {m} / 运行中 {st['live']} 个"


def resource_guard(action: str, name: str = "", spec: dict = None,
                   allow_heavy: bool = False) -> dict:
    """连接/调用前的硬闸门：超温、内存不足、并发过多、重资源组件未确认 → 直接拒。

    这台是 1GB 内存的 Pi 3、被动散热、空载就 63°C。拉无头 chromium 会把它烧到
    SoC 硬关机（journal 整段丢失）。提示词里的"注意资源"是软约束，靠不住，
    所以拦在代码里——模型再想连也连不上。
    """
    st = resource_state()
    bad = []
    if st["temp_c"] is not None and st["temp_c"] >= _TEMP_LIMIT_C:
        bad.append(f"温度 {st['temp_c']:.0f}°C 已达 {_TEMP_LIMIT_C:.0f}°C 上限")
    if st["avail_mb"] is not None and st["avail_mb"] < _MEM_FLOOR_MB:
        bad.append(f"可用内存 {st['avail_mb']:.0f}MB 低于 {_MEM_FLOOR_MB}MB 下限")
    if action == "connect":
        if st["live"] >= _MAX_LIVE:
            bad.append(f"已有 {st['live']} 个 server 在跑（上限 {_MAX_LIVE}），先 mcp_disconnect 一个")
        cmdline = " ".join([str((spec or {}).get("command") or "")] +
                           [str(a) for a in ((spec or {}).get("args") or [])]).lower()
        hit = next((h for h in _HEAVY_HINTS if h in cmdline), None)
        if hit and not allow_heavy:
            bad.append(f"命令含浏览器内核 '{hit}'——1GB 的 Pi 上会烧机，"
                       f"确实要跑就显式传 allow_heavy=true")
    if bad:
        raise MCPError("资源闸门拦截：" + "；".join(bad) + f"。（当前 {_fmt_state(st)}）")
    return st


def get(name: str):
    with _LOCK:
        return _SERVERS.get(name)

def connect(name: str, spec: dict = None, timeout: float = DEFAULT_TIMEOUT,
            allow_heavy: bool = False) -> MCPServer:
    """连接（或复用）一个 server。spec 缺省时从 servers.json 读。"""
    with _LOCK:
        srv = _SERVERS.get(name)
        if srv is not None and srv.alive():
            if not srv.info:
                srv.start(timeout=timeout)
            return srv
        if srv is not None:
            srv.stop()
        cfg = dict(spec or load_specs().get(name) or {})
        url = str(cfg.get("url") or "").strip()
        command = cfg.get("command")
        if not url and not command:
            raise MCPError(f"没有 '{name}' 的配置：请用 mcp_connect 传 url（远程 server）"
                           f"或 command/args（本地子进程），或写进 servers.json。")
        resource_guard("connect", name, cfg, allow_heavy)
        if url:
            from mcp_http import MCPHttpServer  # 延迟导入：mcp_http 反向依赖本模块
            srv = MCPHttpServer(name, url, cfg.get("headers"), timeout=timeout)
        else:
            srv = MCPServer(name, command, cfg.get("args"), cfg.get("env"), cfg.get("cwd"))
        _SERVERS[name] = srv
        try:
            srv.start(timeout=timeout)
        except Exception:
            srv.stop()
            _SERVERS.pop(name, None)
            raise
        return srv


def stop(name: str) -> bool:
    with _LOCK:
        srv = _SERVERS.pop(name, None)
    if srv is None:
        return False
    srv.stop()
    return True


def stop_all():
    with _LOCK:
        names = list(_SERVERS.keys())
    for n in names:
        try:
            stop(n)
        except Exception:
            pass


def running() -> list:
    with _LOCK:
        return [s for s in _SERVERS.values() if s.alive()]


atexit.register(stop_all)
