#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp 技能自测：用本地夹具 server 跑通 连接/清单/调用/错误/超时/杀进程。

    python3 tools/mcp_selftest.py        # 期望输出 全绿
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "mcp"))

import mcp_client as mc  # noqa: E402
import skill as sk  # noqa: E402

FIXTURE = os.path.join(ROOT, "tools", "mcp_test_server.py")
NAME = "selftest"  # 注意：不能以下划线开头，那是「注释键」的保留前缀
TREE = "treekill"
fails = []


def _pgrep() -> list:
    r = subprocess.run(["pgrep", "-f", "dabai-mcp-child"], capture_output=True, text=True)
    return [p for p in r.stdout.split() if p]


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'✔' if ok else '✘'} {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def main():
    # 1. 连接 + initialize
    out = sk.do_connect({"server": NAME, "command": sys.executable, "args": [FIXTURE]})
    check("connect 成功", "已连接" in out, out.splitlines()[0][:90])
    check("serverInfo 正确", "dabai-test-server" in out)
    check("工具清单含 echo/add", "- echo(" in out and "- add(" in out)
    check("清单标注必填参数", "text*" in out)

    # 2. 复用连接（不重启进程）
    srv = mc.get(NAME)
    pid1 = srv._proc.pid
    sk.do_connect({"server": NAME})
    check("重复 connect 复用同一进程", mc.get(NAME)._proc.pid == pid1, f"pid={pid1}")

    # 3. 调用
    r = sk.do_call({"server": NAME, "tool": "echo", "arguments": {"text": "凝练测试"}})
    check("call echo", r == "echo: 凝练测试", r)
    r = sk.do_call({"server": NAME, "tool": "add", "arguments": {"a": 1, "b": 2}})
    check("call add", r == "3.0", r)

    # 4. 参数容错：数组/对象传成字符串
    r = sk.do_call({"server": NAME, "tool": "echo",
                    "arguments": '{"text": "字符串参数"}'})
    check("arguments 传 JSON 字符串也能跑", r == "echo: 字符串参数", r)

    # 5. isError
    r = sk.do_call({"server": NAME, "tool": "boom"})
    check("工具报错带 [工具报错] 前缀", r.startswith("[工具报错]"), r)

    # 6. 未知工具 → 协议 error
    r = sk.do_call({"server": NAME, "tool": "not_exist"})
    check("未知工具返回协议错误", "未知工具" in r, r[:80])

    # 7. 超时（不卡死）
    r = sk.do_call({"server": NAME, "tool": "hang", "arguments": {"seconds": 5}, "timeout": 1})
    check("超时被拦住", "超时" in r, r[:80])

    # 8. 进程死亡后的报错信息
    srv = mc.get(NAME)
    srv._proc.kill()
    srv._proc.wait(timeout=5)
    r = sk.do_call({"server": NAME, "tool": "echo", "arguments": {"text": "x"}})
    check("进程死了能自愈重连或报出退出码", ("退出码" in r) or (r == "echo: x"), r[:90])

    # 9. disconnect 真的杀掉进程
    sk.do_disconnect({"server": "all"})
    check("disconnect 后无存活进程", mc.running() == [], str(mc.running()))

    # 10. servers 列表
    out = sk.do_servers({})
    check("servers 列出配置", NAME in out, out.splitlines()[1][:90] if len(out.splitlines()) > 1 else out)

    # 11. 进程树清理：npx 那类包装器会 fork 孙进程，只杀直接子进程会留孤儿（实测残留 3 个）
    out = sk.do_connect({"server": TREE, "command": sys.executable,
                         "args": [FIXTURE, "--spawn-child"]})
    check("连接带孙进程的 server", "已连接" in out)
    time.sleep(0.5)
    kids = _pgrep()
    check("孙进程已起来", len(kids) >= 1, f"pid={kids}")
    sk.do_disconnect({"server": TREE})
    time.sleep(0.5)
    left = _pgrep()
    check("disconnect 连孙进程一起杀干净", left == [], f"残留={left}")

    # 清理：把自测写进 servers.json 的条目删掉（用 _load_raw 保留 _comment 之类的注释键）
    import json
    specs = mc._load_raw()
    specs.pop(NAME, None)
    specs.pop(TREE, None)
    with open(mc._SPECS_PATH, "w", encoding="utf-8") as f:
        json.dump(specs, f, ensure_ascii=False, indent=2)
    check("自测配置已清理", NAME not in mc.load_specs())

    print()
    if fails:
        print(f"✘ {len(fails)} 项失败：{fails}")
        return 1
    print("✔ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
