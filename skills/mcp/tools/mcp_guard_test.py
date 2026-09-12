# -*- coding: utf-8 -*-
"""资源闸门自测 —— 只调 resource_guard，绝不真启动任何 server（避免把 Pi 再烧一次）。"""
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


def blocked(**kw):
    """返回拦截原因；没被拦返回 None。"""
    try:
        mc.resource_guard(**kw)
        return None
    except mc.MCPError as e:
        return str(e)


LIGHT = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}
HEAVY = {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless"]}

print("=== 一、真实读数 ===")
st = mc.resource_state()
print(f"  {mc._fmt_state(st)}")
check("读到温度", isinstance(st["temp_c"], (int, float)))
check("读到可用内存", isinstance(st["avail_mb"], (int, float)))
check("阈值已定义", st["temp_limit"] == 72.0 and st["mem_floor_mb"] == 180 and st["max_live"] == 2)

print("\n=== 二、正常放行 ===")
check("轻量 server 放行", blocked(action="connect", name="fs", spec=LIGHT) is None,
      blocked(action="connect", name="fs", spec=LIGHT))
check("call 动作放行", blocked(action="call", name="fs") is None)

print("\n=== 三、重资源拦截（本次事故的根因）===")
r = blocked(action="connect", name="pw", spec=HEAVY)
check("playwright 被拦", r is not None, "竟然放行了")
check("原因指向浏览器内核", r and "浏览器内核" in r, r)
check("给出 allow_heavy 出口", r and "allow_heavy" in r, r)
r2 = blocked(action="connect", name="pw", spec={"command": "npx", "args": ["chromium", "--headless"]})
check("裸 chromium 也被拦", r2 is not None and "chromium" in r2, r2)
check("allow_heavy=true 放行", blocked(action="connect", name="pw", spec=HEAVY, allow_heavy=True) is None)

print("\n=== 四、温度/内存阈值（打桩）===")
_real_t, _real_m, _real_run = mc._read_temp_c, mc._read_avail_mb, mc.running
mc._read_temp_c = lambda: 85.0
r = blocked(action="connect", name="fs", spec=LIGHT)
check("85°C 拦截", r is not None and "温度" in r, r)
mc._read_temp_c = lambda: 71.9
check("71.9°C 放行（边界）", blocked(action="connect", name="fs", spec=LIGHT) is None)
mc._read_temp_c = _real_t

mc._read_avail_mb = lambda: 100.0
r = blocked(action="call", name="fs")
check("可用内存 100MB 拦截", r is not None and "可用内存" in r, r)
mc._read_avail_mb = lambda: 180.0
check("刚好 180MB 放行（边界）", blocked(action="call", name="fs") is None)
mc._read_avail_mb = _real_m

print("\n=== 五、并发上限 ===")
mc.running = lambda: [object(), object()]
r = blocked(action="connect", name="fs", spec=LIGHT)
check("已跑 2 个时拦截", r is not None and "上限" in r, r)
check("call 不受并发上限影响", blocked(action="call", name="fs") is None)
mc.running = lambda: [object()]
check("只跑 1 个时放行", blocked(action="connect", name="fs", spec=LIGHT) is None)
mc.running = _real_run

print("\n=== 六、读数失败时不误拦 ===")
mc._read_temp_c = lambda: None
mc._read_avail_mb = lambda: None
check("读不到传感器仍放行", blocked(action="connect", name="fs", spec=LIGHT) is None)
mc._read_temp_c, mc._read_avail_mb = _real_t, _real_m

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}")
sys.exit(1 if fail else 0)
