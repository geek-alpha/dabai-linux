"""project_build 端到端验收脚本（临时；跑完即删）。

验收点：
A2 全链路不报错、spec.json 合法
A3 矩阵精确报出「未验收」「无验收标准」
A4 project_next 单次注入 ≤ 全量 spec 的 25%
A5 FAIL 有结构化反思、PASS 零反思、空证据被拒
"""
import json
import os
import sys
import tempfile

try:  # Windows 控制台默认 GBK，emoji 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # 与 project_build_impl.py 同目录
import project_build_impl as pb  # noqa: E402

ok, bad = [], []


def check(name, cond, extra=""):
    (ok if cond else bad).append(f"{name} {extra}".strip())
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


root = tempfile.mkdtemp(prefix="pb_e2e_")
print("测试目录:", root)

# ── 造 10+ 模块规模的需求清单 ──────────────────────────────
idea_lines = []
for i in range(1, 7):
    idea_lines.append(f"数据表{i}要持久化到数据库并支持迁移")
for i in range(1, 10):
    idea_lines.append(f"核心逻辑{i}要实现状态流转与优先级排序")
for i in range(1, 5):
    idea_lines.append(f"HTTP 接口{i}要暴露给前端调用")
for i in range(1, 6):
    idea_lines.append(f"界面{i}要有列表视图与拖拽交互")
for i in range(1, 4):
    idea_lines.append(f"测试{i}要覆盖核心逻辑的边界情况")
for i in range(1, 3):
    idea_lines.append(f"文档{i}要写清安装与使用步骤")
idea = "\n".join(idea_lines)

r1 = pb.HANDLERS["project_spec_init"]({"idea": idea, "project_dir": root, "name": "E2E看板"})
print("\n--- spec_init ---\n" + r1[:600])
spec_path = os.path.join(root, ".project_build", "spec.json")
check("A2 spec.json 落盘", os.path.exists(spec_path))
with open(spec_path, "r", encoding="utf-8") as f:
    spec = json.load(f)
n_mod, n_feat = len(spec["modules"]), len(spec["features"])
check("A2 模块数 ≥ 10", n_mod >= 10, f"{n_mod} 模块 / {n_feat} 功能")
check("A2 每条功能都有验收", all(f["acceptance"] for f in spec["features"]))
check("A2 模块依赖无悬空",
      not pb._matrix(spec)["bad_deps"], str(pb._matrix(spec)["bad_deps"]))
check("A2 人读视图 SPEC.md 生成", os.path.exists(os.path.join(root, "SPEC.md")))

# ── A4 注入预算 ───────────────────────────────────────────
spec_chars = len(json.dumps(spec, ensure_ascii=False))
r2 = pb.HANDLERS["project_next"]({"project_dir": root})
cur = spec["modules"][0]["id"]
ctx_chars = len(r2)
ratio = round(100.0 * ctx_chars / spec_chars, 1)
check("A4 单模块注入 ≤ 全量 25%", ctx_chars <= spec_chars * 0.25,
      f"{ctx_chars} 字符 vs 全量 {spec_chars}（{ratio}%）")
check("A4 不泄漏其他模块正文",
      all(m["id"] not in r2 for m in spec["modules"] if m["id"] != cur))
check("A2 current_module.md 落盘",
      os.path.exists(os.path.join(root, ".project_build", "current_module.md")))

# ── A5 空证据被拒 ─────────────────────────────────────────
r3 = pb.HANDLERS["project_verify"]({"project_dir": root, "module_id": cur, "evidence": ""})
check("A5 空证据被拒", r3.startswith("❌"), r3[:40])

# ── A5 FAIL → 结构化反思 ──────────────────────────────────
r4 = pb.HANDLERS["project_verify"]({
    "project_dir": root, "module_id": cur, "result": "fail",
    "evidence": "python -m py_compile data/m001.py\nSyntaxError: invalid syntax (m001.py, line 42)"})
check("A5 FAIL 有反思", "反思" in r4 and "语法错误" in r4)
check("A5 FAIL 含关键行", "line 42" in r4)
check("A5 FAIL 状态回退", pb._load_spec(root)[0]["modules"][0]["status"] == "doing")

# ── 正常推进：逐个模块验收 ────────────────────────────────
guard = 0
while guard < 60:
    guard += 1
    s, _ = pb._load_spec(root)
    ready = pb._ready_modules(s)
    if not ready:
        break
    mid = ready[0]["id"]
    out = pb.HANDLERS["project_verify"]({
        "project_dir": root, "module_id": mid,
        "evidence": f"python -m py_compile 通过；pytest -q 3 passed（{mid}）"})
    if not out.startswith("✅"):
        check("A2 逐模块验收", False, out[:120])
        break
s, _ = pb._load_spec(root)
m = pb._matrix(s)
check("A2 全模块推进完成", not pb._ready_modules(s), f"{m['modules_done']}/{m['modules_total']}")
check("A2 覆盖率 100%", m["coverage"] == 100.0, f"{m['coverage']}%")
check("A5 PASS 不产生反思文本",
      not any(mod.get("reflection") for mod in s["modules"]))

# ── A3 矩阵精确点名 ───────────────────────────────────────
s["features"][0]["status"] = "impl"           # 已实现未验收
s["features"][1]["acceptance"] = []           # 无验收标准
pb._save_spec(root, s)
m2 = pb._matrix(s)
check("A3 点名「已实现未验收」", m2["features_impl_unverified"] == [s["features"][0]["id"]],
      str(m2["features_impl_unverified"]))
check("A3 点名「无验收标准」", m2["features_no_acceptance_ids"] == [s["features"][1]["id"]],
      str(m2["features_no_acceptance_ids"]))
check("A3 覆盖率随之下调", m2["coverage"] < 100.0, f"{m2['coverage']}%")
r5 = pb.HANDLERS["project_matrix"]({"project_dir": root, "verbose": False})
check("A3 matrix 输出含警示", "未验收" in r5 and "无验收标准" in r5)

# ── 重复 init 保护 ────────────────────────────────────────
r6 = pb.HANDLERS["project_spec_init"]({"idea": "别的想法", "project_dir": root})
check("A2 已有规格拒绝覆盖", r6.startswith("⚠️"))

print("\n=== 汇总 ===")
print(f"通过 {len(ok)} 项，失败 {len(bad)} 项")
for b in bad:
    print("  FAIL:", b)
print("测试目录保留:", root)
sys.exit(1 if bad else 0)
