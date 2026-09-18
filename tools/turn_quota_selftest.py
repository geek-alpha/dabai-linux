"""每日轮次上限自检：普通用户会被扣到停，管理员/系统身份不被碰。

覆盖两层：
  1) 额度模块本身（turn_quota.py）—— 计数、豁免、跨天清零、落盘、配置读坏兜底；
  2) 钩子是否真的接上 —— 静态核对 server.py 里 _quota_gate 的定义与调用点
     （打字入口 _kickoff_response + 语音入口各一处）。

配置与计数文件全部重定向到临时目录 —— 绝不碰真实 data/turn_quota*.json。

用法：venv/bin/python tools/turn_quota_selftest.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import auth_core          # noqa: E402
import turn_quota         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="quotatest-"))
auth_core.USERS_FILE = TMP / "users.json"
turn_quota.LIMIT_FILE = TMP / "turn_quota.json"
turn_quota.USAGE_FILE = TMP / "turn_quota_usage.json"

OK = []
BAD = []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))


def set_limit(n, exempt=None):
    cfg = {"limit": n}
    if exempt is not None:
        cfg["exempt"] = exempt
    turn_quota.LIMIT_FILE.write_text(json.dumps(cfg), encoding="utf-8")


admin = auth_core.register("管理员", "secret123")          # 首个账号自动成为管理员
user = auth_core.register("普通用户", "secret123")
ADMIN, USER = admin["id"], user["id"]

print("— 豁免判定 —")
check("管理员不限", turn_quota.exempt(ADMIN) is True)
check("普通用户受限", turn_quota.exempt(USER) is False)
check("空 uid（局域网/CLI）不限", turn_quota.exempt("") is True)
check("未注册 uid（系统身份）不限", turn_quota.exempt("u_不存在") is True)
set_limit(5, exempt=[USER])
check("豁免名单生效", turn_quota.exempt(USER) is True)
set_limit(5)

print("— 计数与封顶 —")
turn_quota.reset()
q = turn_quota.consume(USER)
check("首轮放行且计数为 1", q["allowed"] and q["used"] == 1, f"used={q['used']}")
check("剩余额度递减", q["remaining"] == 4, f"remaining={q['remaining']}")
for _ in range(4):
    turn_quota.consume(USER)
q = turn_quota.consume(USER)
check("超额被拒", q["allowed"] is False, f"allowed={q['allowed']}")
check("被拒时计数不再涨", q["used"] == 5, f"used={q['used']}")
check("被拒时 remaining=0", q["remaining"] == 0)
s1 = turn_quota.status(USER)
s2 = turn_quota.status(USER)
check("status 不消耗额度", s1["used"] == s2["used"] == 5, f"used={s1['used']}/{s2['used']}")
check("status 标 limited", s1["limited"] is True)
check("管理员始终放行", turn_quota.consume(ADMIN)["allowed"] is True)
check("管理员 status 不限", turn_quota.status(ADMIN)["limited"] is False)
turn_quota.reset(USER)
check("reset 单个用户后恢复", turn_quota.status(USER)["used"] == 0)
turn_quota.reset()
check("reset 全部后恢复", turn_quota.status(USER)["used"] == 0)

print("— 落盘与跨天 —")
turn_quota.consume(USER)
turn_quota.consume(USER)
on_disk = json.loads(turn_quota.USAGE_FILE.read_text(encoding="utf-8"))
check("计数已落盘（重启不丢）", on_disk["counts"].get(USER) == 2, str(on_disk["counts"]))
check("落盘带日期", bool(on_disk.get("date")))
turn_quota.USAGE_FILE.write_text(
    json.dumps({"date": "2000-01-01", "counts": {USER: 999}}), encoding="utf-8")
check("跨天自动清零", turn_quota.status(USER)["used"] == 0)
turn_quota.USAGE_FILE.write_text(json.dumps({"counts": {USER: 999}}), encoding="utf-8")
check("旧格式（无 date）也清零", turn_quota.status(USER)["used"] == 0)
turn_quota.USAGE_FILE.write_text("不是 JSON", encoding="utf-8")
check("计数文件读坏不炸且按空算", turn_quota.status(USER)["used"] == 0)

print("— 配置兜底 —")
set_limit(0)
check("limit=0 表示不限", turn_quota.status(USER)["limited"] is False)
set_limit(-1)
check("limit<0 表示不限", turn_quota.consume(USER)["allowed"] is True)
turn_quota.LIMIT_FILE.write_text("{坏掉的 JSON", encoding="utf-8")
check("配置读坏回落默认 200", turn_quota.daily_limit() == turn_quota.DEFAULT_LIMIT,
      str(turn_quota.daily_limit()))
turn_quota.LIMIT_FILE.unlink()
check("配置不存在回落默认 200", turn_quota.daily_limit() == turn_quota.DEFAULT_LIMIT)
set_limit(3)

print("— 钩子是否真接上（静态核对 server.py）—")
src = (ROOT / "server.py").read_text(encoding="utf-8")
check("_quota_gate 已定义", "async def _quota_gate(" in src)
lines = [i + 1 for i, l in enumerate(src.splitlines()) if "await _quota_gate(" in l]
check("至少两处调用（打字 + 语音）", len(lines) >= 2, f"行号={lines}")
check("打字入口在 _kickoff_response 内",
      any(5925 < n < 6010 for n in lines), f"行号={lines}")
check("语音入口在 ws 处理循环内",
      any(6600 < n < 6900 for n in lines), f"行号={lines}")
check("只卡用户驱动的一轮（proactive/auto 放行）",
      'if not proactive and msg_source == "chat":' in src)

print(f"\n通过 {len(OK)} / 失败 {len(BAD)}")
if BAD:
    print("失败项：" + "、".join(BAD))
sys.exit(1 if BAD else 0)
