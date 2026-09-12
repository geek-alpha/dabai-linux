# -*- coding: utf-8 -*-
"""孤儿进程回收自测 —— 用假 server（sleep）验证 pid 文件对账机制。

背景：改 skills/mcp/*.py 触发模块重载，_SERVERS 字典清空，但独立进程组的子进程
还活着 —— 工具再也管不到它们（mcp_disconnect 说"没有运行中的"），一直吃内存。
"""
import os
import subprocess
import sys

sys.path.insert(0, "/home/wxf/dabai/skills/mcp")
import mcp_client as mc  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✔ {name}")
    else:
        fail += 1
        print(f"  ✘ {name}  {detail}")


class FakeSrv:
    def __init__(self, name):
        self.name = name


print("=== 一、造一个孤儿（独立进程组）===")
p = subprocess.Popen(["sleep", "300"], start_new_session=True)
pgid = p.pid
mc._write_pidfile("fakesrv", pgid)
print(f"  假 server pgid={pgid}，pid 文件已写")
check("进程组活着", mc._pgid_alive(pgid))

print("\n=== 二、检出 ===")
stale = mc.stale_servers()
hit = [s for s in stale if s["name"] == "fakesrv"]
check("pid 文件里的活进程被检为孤儿", len(hit) == 1 and hit[0]["pgid"] == pgid, stale)

_real_run = mc.running
mc.running = lambda: [FakeSrv("fakesrv")]
check("内存里登记着就不算孤儿", not [s for s in mc.stale_servers() if s["name"] == "fakesrv"])
mc.running = _real_run

print("\n=== 三、回收 ===")
killed = mc.reap_stale()
check("孤儿被清理", any(k["name"] == "fakesrv" for k in killed), killed)
check("进程组已死", not mc._pgid_alive(pgid))
check("pid 文件已删", not os.path.exists(mc._pidfile("fakesrv")))

print("\n=== 四、死进程的 pid 文件不该被当孤儿 ===")
mc._write_pidfile("deadsrv", 999999)  # 不存在的 pgid
check("死 pgid 不报孤儿", not [s for s in mc.stale_servers() if s["name"] == "deadsrv"])
check("死 pgid 的 pid 文件被清掉", not os.path.exists(mc._pidfile("deadsrv")))

print("\n=== 五、重复回收是安全的 ===")
check("再 reap 一次返回空", mc.reap_stale() == [])

try:
    p.kill()
except Exception:
    pass

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}")
sys.exit(1 if fail else 0)
