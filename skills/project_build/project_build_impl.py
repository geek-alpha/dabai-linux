"""project_build —— 从零构建大型项目的结构化能力（规格矩阵 + 增量实现 + 实时验证）。

第一性原理（为什么这样设计）：
- 大项目的 token 黑洞不是"话多"，而是**返工**与**重复注入**：边写边定接口，
  第 10 个模块写完发现第 3 个模块的接口要改，前面全废；
- 所以把"理解"从上下文搬到磁盘：`spec.json` 是唯一真相源，
  `SPEC.md` 只是给人看的渲染，随时可重新生成；
- 上下文按需注入：每次只给「当前模块的契约 + 验收 + 依赖签名」，
  其余模块只留 ID 与一句话——引用 `M-003` 三个字符就够，绝不重述正文；
- 反思只在 FAIL 时触发，且只看结构化摘要（失败类型 + 关键行），
  PASS 时不产生任何反思文本（不花冤枉 token）。

本模块不调用任何 LLM，只用 stdlib 做结构化数据的读写与核算——
省 token 的关键正在于此：流程本身零模型成本。
"""
from __future__ import annotations

import json
import os
import re
import time

# ── 状态文件 ──────────────────────────────────────────────
STATE_DIR = ".project_build"
SPEC_FILE = "spec.json"
NEXT_MD = "current_module.md"
SPEC_MD = "SPEC.md"

# 单次注入的字符上限（超限截断——注入越长越费 token，宁可让模型主动再问一次）
DEFAULT_BUDGET = 6000

# 模块类别与构建顺序：数据 → 核心 → 接口 → 界面 → 测试 → 文档
KIND_ORDER = ("data", "core", "api", "ui", "test", "doc")
KIND_TITLE = {
    "data": "数据层", "core": "核心逻辑", "api": "接口层",
    "ui": "界面层", "test": "测试", "doc": "文档",
}
KIND_DIR = {
    "data": "data", "core": "core", "api": "api",
    "ui": "ui", "test": "tests", "doc": "docs",
}

# 真实契约依赖：每一层「需要读谁的对外承诺」，而不是「构建先后顺序」。
# 这个区别是并行的生死线：
# - 老实现按「上一阶段」连成一条串行链 → 任何项目都只能一个模块一个模块地做，
#   写集毫不相干的 tests/* 与 docs/* 被顺序绑死，子智能体白等；
# - 真实依赖下，测试依赖的是「被测的核心」而不是界面，文档依赖的是「核心用法」
#   而不是测试结果 → core 一完成，ui/test/doc 三者就能并行开工。
_KIND_DEPS = {
    "data": (),
    "core": ("data",),
    "api": ("core", "data"),
    "ui": ("api", "core", "data"),
    "test": ("core", "data"),
    "doc": ("core", "data"),
}

# 关键词 → 类别（从零构建时用于把需求条目归类，决定模块划分与依赖顺序）
KIND_KEYWORDS = (
    ("data", ("数据", "存储", "数据库", "模型", "表结构", "持久化", "缓存", "schema", "database")),
    ("ui", ("界面", "页面", "前端", "ui", "交互", "按钮", "窗口", "显示", "可视化", "面板", "dashboard")),
    ("api", ("接口", "api", "服务", "路由", "endpoint", "http", "websocket", "协议", "客户端")),
    ("test", ("测试", "test", "用例", "断言", "压测", "基准", "benchmark")),
    ("doc", ("文档", "说明", "readme", "注释", "手册", "教程")),
)

# 拆需求条目：换行 / 分号 / 中文顿号列举 / 编号列表 / 句末句号
_SPLIT_RE = re.compile(r"[\n;；]+|(?<=[。！？])\s*|(?:^|\s)[-*·]\s+|\d+[.、)]\s*")
# 需求条目里常见的"连接词"开头，切分后需要剥掉
_LEAD_RE = re.compile(r"^[（(【\[]?\s*(?:要|需要|能|可以|支持|并且|然后|接着|以及|还有|包括|实现|做一个|做一个?|要有)\s*")
# 功能描述里抓文件名/路径线索（如 "config.json"、"src/app.py"）
_PATH_RE = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,6}")


# ── 通用小工具 ────────────────────────────────────────────
def _err(msg: str) -> str:
    return f"❌ {msg}"


def _now() -> float:
    return time.time()


def _s(v, default: str = "") -> str:
    """安全转字符串并去空白。"""
    if v is None:
        return default
    return str(v).strip()


def _slug(text: str, fallback: str = "item") -> str:
    """中文/符号 → 安全的文件名片段（保留中文，替换分隔符）。"""
    t = re.sub(r"[\s/\\:*?\"<>|]+", "-", _s(text)).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return (t[:40] or fallback)


def _resolve_dir(args: dict, key: str = "project_dir") -> str:
    """取项目目录（默认当前工作目录），并保证其存在。"""
    p = _s(args.get(key)) or os.getcwd()
    p = os.path.abspath(p)
    os.makedirs(p, exist_ok=True)
    return p


def _spec_path(pdir: str) -> str:
    return os.path.join(pdir, STATE_DIR, SPEC_FILE)


def _atomic_write(path: str, text: str) -> None:
    """原子写：先写 .tmp 再 replace，避免中途崩溃留下半截文件。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_spec(pdir: str):
    """读 spec.json。返回 (spec, error_text)；error_text 非空表示失败。"""
    p = _spec_path(pdir)
    if not os.path.exists(p):
        return None, (f"未找到规格文件 {p}。请先用 project_spec_init 初始化项目规格"
                      f"（一句话想法即可），再执行本操作。")
    try:
        with open(p, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except Exception as e:
        return None, f"规格文件损坏无法解析（{p}）：{e}。可用备份 .tmp 或重新 project_spec_init。"
    if not isinstance(spec, dict) or not isinstance(spec.get("modules"), list):
        return None, f"规格文件结构不合法（{p}）：缺少 modules 列表。"
    spec.setdefault("features", [])
    spec.setdefault("log", [])
    return spec, ""


def _save_spec(pdir: str, spec: dict) -> None:
    spec["updated"] = _now()
    _atomic_write(_spec_path(pdir), json.dumps(spec, ensure_ascii=False, indent=2))
    try:
        _atomic_write(os.path.join(pdir, SPEC_MD), _render_spec_md(spec))
    except Exception:
        pass  # 人读视图失败不影响机读真相源


def _find_module(spec: dict, mid: str):
    for m in spec.get("modules", []):
        if m.get("id") == mid:
            return m
    return None


def _find_feature(spec: dict, fid: str):
    for f in spec.get("features", []):
        if f.get("id") == fid:
            return f
    return None


def _log(spec: dict, action: str, detail: str = "") -> None:
    """操作日志（只留最近 200 条，防止 spec 无限膨胀）。"""
    spec.setdefault("log", []).append(
        {"ts": _now(), "action": action, "detail": _s(detail)[:200]})
    if len(spec["log"]) > 200:
        spec["log"] = spec["log"][-200:]


def _next_id(items: list, prefix: str, width: int = 3) -> str:
    """生成下一个不冲突的 ID（M-001 / F-001）。"""
    used = {i.get("id") for i in items}
    n = 1
    while f"{prefix}-{n:0{width}d}" in used:
        n += 1
    return f"{prefix}-{n:0{width}d}"


# ── 需求拆解与模块规划（纯启发式，零模型成本）────────────
def _split_requirements(idea: str) -> list:
    """把一段想法拆成需求条目。

    规则：换行 / 分号 / 句末标点 / 编号列表 / 短横线列表 都是分隔符；
    拆完剥掉"需要/支持/实现"这类引导词，去重、丢弃过短碎片。
    """
    raw = _SPLIT_RE.split(_s(idea))
    out = []
    for seg in raw:
        seg = _LEAD_RE.sub("", _s(seg)).strip("。.、,， 　")
        if len(seg) < 2:
            continue
        if seg in out:
            continue
        out.append(seg)
    return out or [_s(idea) or "未描述的目标"]


def _classify(text: str) -> str:
    """把需求条目归类到构建阶段（决定模块划分与依赖顺序）。"""
    low = _s(text).lower()
    for kind, kws in KIND_KEYWORDS:
        if any(k in low for k in kws):
            return kind
    return "core"


def _impl_path(kind: str, mid: str) -> str:
    """模块实现文件的建议路径（可预测、无中文、便于 py_compile 校验）。"""
    low = mid.lower()
    if kind == "test":
        return f"tests/test_{low}.py"
    if kind == "doc":
        return f"docs/{low}.md"
    return f"{KIND_DIR[kind]}/{low}.py"


def _acceptance_for(kind: str, fid: str, desc: str, path: str) -> list:
    """给每条功能生成可执行验收检查点（占位但结构完整，实现时按需细化）。"""
    if kind == "doc":
        return [f"{path} 存在且含 {fid} 对应章节"]
    if kind == "test":
        return [f"运行通过：pytest {path} -q"]
    checks = [f"{path} 存在且 py_compile 通过"]
    checks.append(f"{fid} 行为断言：{_s(desc)[:40]} —— 写明执行命令与期望输出，实际跑通")
    return checks


def _dep_modules(kind: str, by_kind: dict) -> list:
    """按契约依赖挑前置模块：取最近一层存在的那层（该层全部模块——契约要读全）。"""
    for dep_kind in _KIND_DEPS.get(kind, ()):
        mods = by_kind.get(dep_kind)
        if mods:
            return [m["id"] for m in mods]
    return []


def _plan_from_idea(idea: str, name: str, chunk: int = 4) -> dict:
    """想法 → 规格骨架（功能清单 F-xxx + 模块 M-xxx + 依赖链 + 验收）。

    这是"面面俱到"的可计算基础：每条需求都有 ID，每个 ID 都有归属模块与验收标准，
    后续 project_matrix 能精确算出覆盖率、无用例、未验收的条目。

    模块划分：按构建阶段归类（数据→核心→接口→界面→测试→文档）；
    同阶段功能超过 chunk 条就切成多个模块（大型项目的核心逻辑通常最多）。

    依赖按「真实契约」而非「构建顺序」：测试依赖被测的核心（不依赖界面）、
    文档依赖核心用法（不依赖测试）→ core 完成后 ui/test/doc 可并行派发。
    同层模块之间互不依赖（各自独立文件，写集不冲突即可并行）。
    """
    reqs = _split_requirements(idea)
    buckets = {}  # kind -> [(fid, desc), ...]
    for i, desc in enumerate(reqs, 1):
        buckets.setdefault(_classify(desc), []).append((f"F-{i:03d}", desc))

    chunk = max(1, int(chunk or 4))
    modules, features = [], []
    by_kind: dict = {}
    for kind in KIND_ORDER:
        items = buckets.get(kind)
        if not items:
            continue
        for start in range(0, len(items), chunk):
            group = items[start:start + chunk]
            mid = f"M-{len(modules) + 1:03d}"
            path = _impl_path(kind, mid)
            fids = []
            for fid, desc in group:
                fids.append(fid)
                features.append({
                    "id": fid, "desc": desc, "module": mid, "kind": kind,
                    "status": "todo", "evidence": "",
                    "acceptance": _acceptance_for(kind, fid, desc, path),
                })
            mod = {
                "id": mid, "name": f"{KIND_TITLE[kind]}",
                "kind": kind, "dir": KIND_DIR[kind], "deps": [],
                "provides": fids, "files": [path],
                "status": "todo", "evidence": "", "reflection": "",
            }
            modules.append(mod)
            by_kind.setdefault(kind, []).append(mod)
    # 依赖统一在最后补：只依赖「需要读其契约」的前置层，
    # 同层模块之间互不依赖 → 写集不冲突就能并行。
    for mod in modules:
        mod["deps"] = _dep_modules(mod["kind"], by_kind)
    return {
        "project": _s(name) or "未命名项目",
        "goal": _s(idea),
        "created": _now(), "updated": _now(),
        "modules": modules, "features": features, "log": [],
    }


# ── 渲染与核算 ────────────────────────────────────────────
_STATUS_MARK = {"todo": "⬜", "doing": "🔄", "impl": "🔨", "verified": "✅",
                "done": "✅", "blocked": "🚫"}


def _matrix(spec: dict) -> dict:
    """覆盖率核算——"面面俱到"在这里变成可计算的数字。"""
    mods = spec.get("modules", [])
    feats = spec.get("features", [])
    verified = [f for f in feats if f.get("status") == "verified"]
    impl = [f for f in feats if f.get("status") == "impl"]
    todo = [f for f in feats if f.get("status") not in ("verified", "impl")]
    no_acc = [f for f in feats if not f.get("acceptance")]
    ids = {m.get("id") for m in mods}
    dangling = [f.get("id") for f in feats if f.get("module") not in ids]
    bad_deps = [f"{m.get('id')}→{d}" for m in mods for d in (m.get("deps") or []) if d not in ids]
    return {
        "modules_total": len(mods),
        "modules_done": len([m for m in mods if m.get("status") in ("done", "verified")]),
        "features_total": len(feats),
        "features_verified": len(verified),
        "features_impl": len(impl),
        "features_todo": len(todo),
        "features_no_acceptance": len(no_acc),
        "features_impl_unverified": [f["id"] for f in impl],
        "features_todo_ids": [f["id"] for f in todo],
        "features_no_acceptance_ids": [f["id"] for f in no_acc],
        "dangling_module_refs": dangling,
        "bad_deps": bad_deps,
        "coverage": round(100.0 * len(verified) / len(feats), 1) if feats else 0.0,
    }


def _render_matrix(spec: dict, m: dict = None) -> str:
    m = m or _matrix(spec)
    lines = [
        f"📊 覆盖率 {m['coverage']}% —— 功能 {m['features_verified']}/{m['features_total']} 已验证"
        f"（实现未验收 {m['features_impl']}，未开始 {m['features_todo']}）",
        f"模块 {m['modules_done']}/{m['modules_total']} 完成",
    ]
    if m["features_no_acceptance_ids"]:
        lines.append(f"⚠️ 无验收标准的功能：{', '.join(m['features_no_acceptance_ids'])}")
    if m["features_impl_unverified"]:
        lines.append(f"⚠️ 已实现但未验收：{', '.join(m['features_impl_unverified'])}")
    if m["dangling_module_refs"]:
        lines.append(f"🚫 悬空模块引用：{', '.join(m['dangling_module_refs'])}")
    if m["bad_deps"]:
        lines.append(f"🚫 无效依赖：{', '.join(m['bad_deps'])}")
    return "\n".join(lines)


def _render_spec_md(spec: dict) -> str:
    """人读视图（可随时从 spec.json 重新生成，不作为真相源）。"""
    m = _matrix(spec)
    out = [f"# {spec.get('project')} —— 规格（自动生成，勿手改）", "",
           f"**目标**：{spec.get('goal')}", "",
           f"**进度**：{_render_matrix(spec, m)}", "", "## 模块", ""]
    for mod in spec.get("modules", []):
        out.append(f"- {_STATUS_MARK.get(mod.get('status'), '?')} `{mod['id']}` {mod['name']}"
                   f"（{mod['kind']}） 依赖：{', '.join(mod.get('deps') or []) or '无'}")
        out.append(f"  - 文件：{', '.join(mod.get('files') or [])}")
        out.append(f"  - 承诺功能：{', '.join(mod.get('provides') or [])}")
    out += ["", "## 功能清单", ""]
    for f in spec.get("features", []):
        out.append(f"- {_STATUS_MARK.get(f.get('status'), '?')} `{f['id']}` [{f['module']}] {f['desc']}")
        for a in f.get("acceptance") or []:
            out.append(f"  - 验收：{a}")
        if f.get("evidence"):
            out.append(f"  - 证据：{f['evidence']}")
    return "\n".join(out) + "\n"


# ── 按需注入：只给当前模块的上下文（省 token 的核心）──────
def _deps_done(spec: dict, mod: dict) -> bool:
    for d in mod.get("deps") or []:
        dm = _find_module(spec, d)
        if not dm or dm.get("status") not in ("done", "verified"):
            return False
    return True


def _ready_modules(spec: dict) -> list:
    """就绪 = 自身未完成且依赖全部完成（按构建顺序返回）。"""
    out = []
    for m in spec.get("modules", []):
        if m.get("status") in ("done", "verified"):
            continue
        if _deps_done(spec, m):
            out.append(m)
    return out


def _module_ctx(spec: dict, mod: dict, budget: int = DEFAULT_BUDGET) -> str:
    """生成单模块上下文。只含本模块 + 依赖的对外承诺，绝不含其他模块正文。

    超预算时逐级降级：先截功能描述 → 再省验收明细 → 最后省依赖说明。
    """
    feats = [f for f in spec.get("features", []) if f.get("module") == mod["id"]]
    lines = [f"【当前模块 {mod['id']} {mod['name']}】状态：{mod.get('status')}",
             f"目标：兑现 {'/'.join(mod.get('provides') or [])} 共 {len(feats)} 条功能"]
    for f in feats:
        lines.append(f"- `{f['id']}` {_s(f.get('desc'))[:120]}")
    lines.append(f"涉及文件（只改这里）：{', '.join(mod.get('files') or [])}")
    if mod.get("deps"):
        deps = [f"{d} {(_find_module(spec, d) or {}).get('name', '')}"
                f"→ 承诺 {','.join((_find_module(spec, d) or {}).get('provides') or [])}"
                for d in mod["deps"]]
        lines.append("依赖（已完成，按其契约调用即可，禁止整读其实现）：" + "；".join(deps))
    acc = []
    for f in feats:
        for a in f.get("acceptance") or []:
            acc.append(f"- {a}")
    lines.append("验收（逐条跑通，把「命令 + 关键输出」作为 evidence 交给 project_verify）：")
    lines += acc
    lines.append("纪律：① 先写出本模块对外签名/契约，再填实现；② 只改上面列出的文件，"
                 "不要顺手重构别的模块；③ 写完立刻 project_verify(module_id, evidence) —— "
                 "没跑命令不许说完成；④ 需要别的模块细节时用 ID 引用，不要把全文读进上下文。")
    text = "\n".join(lines)
    if len(text) <= budget:
        return text
    # 降级 1：截功能描述
    for i, f in enumerate(feats):
        if len(text) <= budget:
            break
        text = text.replace(_s(f.get("desc"))[:120], _s(f.get("desc"))[:36] + "…")
    if len(text) <= budget:
        return text
    # 降级 2：省验收明细（保留条数提示）
    text = re.sub(r"\n验收（[^\n]*\n(?:- [^\n]*\n)+",
                  f"\n验收：{len(acc)} 条（详见 spec.json，跑完把证据交给 project_verify）\n", text)
    if len(text) <= budget:
        return text
    # 降级 3：硬截断（宁可让模型主动再问，也不把上下文塞爆）
    return text[:budget] + f"\n…（已按 {budget} 字预算截断；需要细节请读 spec.json 或用 project_matrix）"


# ── 实时反思：只在 FAIL 时触发，只看结构化摘要 ─────────────
_FAIL_KINDS = (
    ("语法错误", ("syntaxerror", "invalid syntax", "indentationerror", "py_compile")),
    ("导入失败", ("importerror", "modulenotfound", "no module named", "circular import")),
    ("测试失败", ("assertionerror", "assert", "failed", "pytest")),
    ("超时", ("timeout", "timed out", "超时")),
    ("接口不匹配", ("typeerror", "attributeerror", "unexpected keyword", "签名")),
    ("依赖/路径缺失", ("no such file", "filenotfound", "not found", "找不到", "不存在")),
)
_FAIL_ADVICE = {
    "语法错误": "只对报错文件跑 py_compile 定位行号，改那一行；不要重写整个文件。",
    "导入失败": "核对文件路径与包内 __init__.py，确认没有循环导入；用相对导入或加 sys.path。",
    "测试失败": "单独跑失败用例（pytest -k），对比断言的实际值与期望值，先判断是代码错还是用例错。",
    "超时": "缩小输入规模或加超时，检查是否有死循环/阻塞等待；先跑最小可复现样例。",
    "接口不匹配": "对照依赖模块的契约签名（spec.json 的 provides），改调用方，别乱改被调用方。",
    "依赖/路径缺失": "确认前置模块文件真的存在；缺什么先补什么，不要用 try/except 掩盖。",
}
_DEFAULT_ADVICE = "读证据里第一条报错，定位到具体「文件:行号」，只改那一处，改完立即重跑验收。"


def _classify_failure(evidence: str) -> str:
    low = _s(evidence).lower()
    for kind, kws in _FAIL_KINDS:
        if any(k in low for k in kws):
            return kind
    return "未分类"


def _key_lines(evidence: str, limit: int = 5) -> list:
    """从证据里抽关键行：报错行 / 失败行 / 异常行，每行截断——绝不重述全文。"""
    keys = ("error", "traceback", "failed", "exception", "❌", "assert", "timeout", "报错", "失败")
    out = []
    for raw in _s(evidence).splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(k in line.lower() for k in keys):
            out.append(line[:140])
        if len(out) >= limit:
            break
    return out or [_s(evidence)[:140]]


def _reflect(spec: dict, mod: dict, evidence: str, own_note: str = "") -> str:
    """结构化反思摘要：失败类型 + 关键行 + 下一步。PASS 时根本不调用本函数。"""
    kind = _classify_failure(evidence)
    out = [f"🔁 反思（结构化摘要，不重述全文）",
           f"失败类型：{kind}",
           f"失败位置：{mod['id']} {mod['name']}（文件 {', '.join(mod.get('files') or [])}）",
           "关键行："]
    out += [f"  - {ln}" for ln in _key_lines(evidence)]
    out.append("下一步：")
    out.append(f"  - {_FAIL_ADVICE.get(kind, _DEFAULT_ADVICE)}")
    if own_note:
        out.append(f"  - 自查：{_s(own_note)[:200]}")
    out.append("  - 纪律：同一错误连续出现两次 → 停下诊断根因，不要换着方法反复硬试；"
               "修完只重跑本模块验收。")
    return "\n".join(out)


# ── 工具 1：初始化规格 ────────────────────────────────────
def project_spec_init(args: dict) -> str:
    """一句话想法 → spec.json（唯一真相源）+ SPEC.md（人读视图）+ 目录骨架。"""
    idea = _s(args.get("idea"))
    if not idea:
        return _err("请用 idea 参数给出项目想法（一句话或一段需求清单都行，我来拆成功能与模块）。")
    pdir = _resolve_dir(args)
    sp = _spec_path(pdir)
    if os.path.exists(sp) and not args.get("overwrite"):
        spec, _ = _load_spec(pdir)
        if spec:
            return (f"⚠️ 该项目已有规格（{sp}），未覆盖。当前状态：\n{_render_matrix(spec)}\n"
                    f"继续构建用 project_next；确实要重建请传 overwrite=true（会丢失已有进度）。")
    name = _s(args.get("name")) or os.path.basename(pdir)
    spec = _plan_from_idea(idea, name)
    if args.get("scaffold_dirs", True):
        for mod in spec["modules"]:
            try:
                os.makedirs(os.path.join(pdir, mod["dir"]), exist_ok=True)
            except Exception:
                pass
    _log(spec, "spec_init", f"{len(spec['modules'])} 模块 / {len(spec['features'])} 功能")
    _save_spec(pdir, spec)

    lines = [f"✅ 规格已建立：{sp}", _render_matrix(spec),
             "模块划分（功能 ID 是后续唯一的引用方式，不要重述需求原文）："]
    for mod in spec["modules"]:
        lines.append(f"- `{mod['id']}` {mod['name']}（{mod['kind']}，依赖 "
                     f"{', '.join(mod.get('deps') or []) or '无'}）：{', '.join(mod['provides'])}")
    lines.append("下一步：project_next 取第一个模块的上下文（只注入该模块，不重述全量需求）。"
                 "如果模块划分或验收标准不符合你的理解，直接说，我改 spec.json。")
    return "\n".join(lines)


# ── 工具 2：取下一个模块的上下文 ──────────────────────────
def project_next(args: dict) -> str:
    """算下一个该做的模块，只注入它的契约+验收+依赖——其他模块只留 ID。"""
    pdir = _resolve_dir(args)
    spec, err = _load_spec(pdir)
    if err:
        return _err(err)
    mid = _s(args.get("module_id"))
    if mid:
        mod = _find_module(spec, mid)
        if not mod:
            return _err(f"没有模块 {mid}。现有模块：{', '.join(m['id'] for m in spec['modules'])}")
    else:
        ready = _ready_modules(spec)
        if not ready:
            pending = [m["id"] for m in spec["modules"] if m.get("status") not in ("done", "verified")]
            if not pending:
                return f"🎉 全部模块已完成。\n{_render_matrix(spec)}"
            return (f"🚫 没有就绪模块（依赖未完成）：{', '.join(pending)}\n"
                    f"先用 project_matrix 看全局，优先完成被依赖的模块。")
        mod = ready[0]
    if mod.get("status") == "todo":
        mod["status"] = "doing"
    budget = int(args.get("budget") or DEFAULT_BUDGET)
    text = _module_ctx(spec, mod, budget)
    _log(spec, "next", mod["id"])
    _save_spec(pdir, spec)
    try:
        _atomic_write(os.path.join(pdir, STATE_DIR, NEXT_MD), text)
    except Exception:
        pass
    return text


# ── 工具 3：记录验收结果（并触发反思）─────────────────────
def project_verify(args: dict) -> str:
    """记录验收证据。evidence 必填——没跑命令、没有输出就不算完成。"""
    pdir = _resolve_dir(args)
    spec, err = _load_spec(pdir)
    if err:
        return _err(err)
    mid = _s(args.get("module_id"))
    fid = _s(args.get("feature_id"))
    evidence = _s(args.get("evidence"))
    result = _s(args.get("result") or "pass").lower()
    failed = result in ("fail", "failed", "false", "no", "0", "失败", "不通过")

    feat = None
    if fid:
        feat = _find_feature(spec, fid)
        if not feat:
            return _err(f"没有功能 {fid}。现有功能：{', '.join(f['id'] for f in spec['features'][:20])}")
        mid = mid or _s(feat.get("module"))
    if not mid:
        return _err("请给出 module_id 或 feature_id（验收哪个模块/功能）。")
    mod = _find_module(spec, mid)
    if not mod:
        return _err(f"没有模块 {mid}。")
    if not evidence:
        return _err("evidence 不能为空——没跑命令、没有输出摘要就不算验收通过。"
                    "把「执行的命令 + 关键输出」贴进 evidence（几十字即可），"
                    "这是防止「自认为做完」的唯一硬约束。")

    ev = evidence[:600]
    touched = [feat] if feat else [f for f in spec["features"] if f.get("module") == mid]

    if failed:
        for f in touched:
            if f.get("status") == "verified":
                f["status"] = "doing"
            f["evidence"] = ev
        mod["status"] = "doing"
        mod["evidence"] = ev
        refl = _reflect(spec, mod, evidence, _s(args.get("reflection")))
        mod["reflection"] = refl
        _log(spec, "verify_fail", f"{mid} {'/'.join(f['id'] for f in touched)}")
        _save_spec(pdir, spec)
        return (f"🚫 {mid} 验收未通过（状态回退 doing，已记录反思）\n{refl}\n"
                f"提示：修完只重跑本模块验收，不要重跑全项目。")

    for f in touched:
        f["status"] = "verified"
        f["evidence"] = ev
    remaining = [f for f in spec["features"] if f.get("module") == mid and f.get("status") != "verified"]
    mod["status"] = "doing" if remaining else "done"
    mod["evidence"] = ev
    mod["reflection"] = ""
    _log(spec, "verify_pass", f"{mid} {'/'.join(f['id'] for f in touched)}")
    _save_spec(pdir, spec)

    out = [f"✅ {mid} 验收通过：{', '.join(f['id'] for f in touched)}（证据已记录）",
           _render_matrix(spec)]
    ready = _ready_modules(spec)
    if ready:
        out.append(f"下一个就绪模块：{ready[0]['id']} {ready[0]['name']}"
                   f" → project_next 取上下文（只注入该模块）")
    elif all(m.get("status") in ("done", "verified") for m in spec["modules"]):
        out.append("🎉 全部模块完成。建议最后跑一次 project_matrix 核对覆盖率是否 100%。")
    return "\n".join(out)


# ── 工具 4：覆盖率核算 ────────────────────────────────────
def project_matrix(args: dict) -> str:
    """全局核算：覆盖率、已实现未验收、无用例、悬空引用、阻塞模块。"""
    pdir = _resolve_dir(args)
    spec, err = _load_spec(pdir)
    if err:
        return _err(err)
    m = _matrix(spec)
    verbose = bool(args.get("verbose"))
    out = [f"项目：{spec.get('project')}（{spec.get('goal', '')[:60]}）", _render_matrix(spec, m)]
    out.append("模块进度：")
    for mod in spec.get("modules", []):
        mark = _STATUS_MARK.get(mod.get("status"), "?")
        out.append(f"- {mark} `{mod['id']}` {mod['name']}"
                   + ("" if verbose else f"（{len(mod.get('provides') or [])} 功能）"))
        if verbose:
            out.append(f"    - 依赖：{', '.join(mod.get('deps') or []) or '无'}｜"
                       f"文件：{', '.join(mod.get('files') or [])}")
            for f in spec.get("features", []):
                if f.get("module") == mod["id"]:
                    out.append(f"    - {_STATUS_MARK.get(f.get('status'), '?')} {f['id']} {f['desc'][:50]}")
    todo = m["features_todo_ids"]
    if todo:
        out.append(f"未完成功能：{', '.join(todo[:30])}" + ("…" if len(todo) > 30 else ""))
    ready = _ready_modules(spec)
    out.append(f"下一步：{ready[0]['id'] + ' ' + ready[0]['name'] if ready else '无就绪模块（检查依赖）'}"
               f" → project_next")
    if m["coverage"] < 100.0:
        out.append("达标条件：覆盖率 100% 且无「已实现未验收」「无验收标准」条目——"
                   "这才是「面面俱到」，不是感觉齐全。")
    return "\n".join(out)


# ── 工具 5：并行派发（把无写集冲突的模块交给配置好的子智能体）───
def _norm_path(p: str) -> str:
    return str(p or "").replace("\\", "/").strip().strip("/")


def _write_set(mod: dict) -> set:
    """模块的写集：优先用精确文件清单，没有文件才退回模块目录。

    不能拿「目录」当默认粒度：同层两个模块各写 core/m-001.py 与 core/m-002.py，
    目录都是 core，按目录判就会被误判成交叉——明明文件完全不同却永远无法并行。
    """
    out = {_norm_path(f) for f in (mod.get("files") or []) if str(f).strip()}
    if not out:
        d = _norm_path(mod.get("dir"))
        if d:
            out.add(d)
    return out


def _overlap(a: set, b: set) -> bool:
    """写集是否相交（含目录前缀关系：src/core 与 src/core/a.py 算相交）。"""
    for x in a:
        for y in b:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def _pick_parallel(ready: list, count: int) -> tuple:
    """从就绪模块里挑一批「写集互不相交」的，返回 (选中, 因冲突被跳过)。

    并行安全的第一性原理判据就是写集：两个模块改同一文件/同一目录，
    同时派出去必然互相覆盖（后写的赢），比串行还慢——返工 + 冲突。
    """
    picked: list = []
    skipped: list = []
    used: set = set()
    for m in ready:
        ws = _write_set(m)
        if not ws:
            skipped.append((m["id"], "未声明涉及文件"))
            continue
        if _overlap(ws, used):
            skipped.append((m["id"], "与已选模块写集相交"))
            continue
        picked.append(m)
        used |= ws
        if len(picked) >= count:
            break
    return picked, skipped


def _recommend_profile(kind: str) -> str:
    """按模块类型推荐档案（test→tester、doc→writer…）；拿不到就交给 general。"""
    try:
        from agent_profiles import recommend_for_kind
        return recommend_for_kind(kind)
    except Exception:
        return ""


def _dispatch_task(spec: dict, mod: dict, pdir: str) -> str:
    """单个模块的委派任务规范：目标/范围/契约/验收/回报格式——一次说清，不让子智能体猜。"""
    head = [f"【项目构建 · 模块 {mod['id']}《{mod['name']}》】",
            f"项目：{spec.get('project')}｜工作目录：{pdir}",
            "你是这个模块的唯一负责人：只完成它，不要碰其他模块的文件。"]
    body = _module_ctx(spec, mod, 3000)
    tail = ["",
            "【回报格式（必须按这四行交回，主智能体据此验收）】",
            "① 改动文件清单（完整相对路径）",
            "② 验收命令原文（可复制直接执行）",
            "③ 每条命令的关键输出（原文，不要转述）",
            "④ 未完成项与存疑点（没有就写「无」）",
            "注意：不要自己调用 project_verify——验收由主智能体统一记录。"]
    return "\n".join(head + [body] + tail)


def project_dispatch(args: dict) -> str:
    """把一批「依赖已就绪 + 写集互不相交」的模块，派给按角色配置好的子智能体并行做。

    为什么必须查写集：并行的收益来自同时干活，风险也来自同时写同一批文件。
    只按「依赖就绪」派活是不够的——依赖没写在 spec 里但文件重叠的两个模块，
    并行等于互相覆盖。
    """
    pdir = _resolve_dir(args)
    spec, err = _load_spec(pdir)
    if err:
        return _err(err)
    all_ready = _ready_modules(spec)
    # 已在做的（doing/impl）不能再派：同一模块派两次 = 两个子智能体写同一批文件，
    # 后写的赢、先写的白干——这正是并行最贵的失败方式。
    busy = [m["id"] for m in all_ready if m.get("status") in ("doing", "impl")]
    ready = [m for m in all_ready if m.get("status") not in ("doing", "impl")]
    if not ready:
        pending = [m["id"] for m in spec["modules"] if m.get("status") not in ("done", "verified")]
        if not pending:
            return f"🎉 全部模块已完成，没有可派发的。\n{_render_matrix(spec)}"
        if busy:
            return (f"⏳ 就绪模块都已在执行中：{', '.join(busy)}（等它们汇报后再派下一批）。\n"
                    "看进度用 sub_agents_list；它们交回后跑验收命令，再 project_verify。")
        return (f"🚫 没有就绪模块（依赖未完成）：{', '.join(pending)}\n"
                "先用 project_next 做被依赖的模块；依赖链是串行的，不要硬并行。")
    try:
        count = int(args.get("count") or 2)
    except Exception:
        count = 2
    count = max(1, min(count, 8))
    profile = _s(args.get("profile"))
    picked, skipped = _pick_parallel(ready, count)
    if not picked:
        return _err("就绪模块之间写集全部相交（改同一批文件/目录），并行不安全："
                    "用 project_next 串行做，或先把模块文件范围拆开再派。"
                    + ("\n跳过明细：" + "；".join(f"{i}（{r}）" for i, r in skipped) if skipped else ""))

    workers = []
    plan = []
    for mod in picked:
        pid = profile or _recommend_profile(mod.get("kind"))
        workers.append({
            "task": _dispatch_task(spec, mod, pdir),
            "title": f"{mod['id']} {mod['name']}",
            "profile": pid,
        })
        plan.append(f"- {mod['id']} {mod['name']}（{mod['kind']}）→ 档案 {pid or 'general'}"
                    f"｜文件：{', '.join(mod.get('files') or [])}")

    if args.get("dry_run"):
        out = [f"📋 派发预演（未真正下发）：{len(workers)} 个模块可并行", *plan]
        if skipped:
            out.append("因写集冲突/无文件被跳过：" + "；".join(f"{i}（{r}）" for i, r in skipped))
        if busy:
            out.append(f"已在执行中（本次不再派）：{', '.join(busy)}")
        out.append("确认后重发（去掉 dry_run）即真正派发。")
        return "\n".join(out)

    for mod in picked:
        if mod.get("status") == "todo":
            mod["status"] = "doing"
    _log(spec, "dispatch", f"{','.join(m['id'] for m in picked)}（{len(picked)} 并行）")
    _save_spec(pdir, spec)

    payload = {
        "__sub_agent_spawn__": True,
        "workers": workers,
        "message": (f"已派发 {len(workers)} 个模块并行：{', '.join(m['id'] for m in picked)}；"
                    "完成后会自动汇报，拿它们的回报跑验收命令，再 project_verify。"),
    }
    return json.dumps(payload, ensure_ascii=False)


# ── 注册表 ────────────────────────────────────────────────
HANDLERS = {
    "project_spec_init": project_spec_init,
    "project_next": project_next,
    "project_verify": project_verify,
    "project_matrix": project_matrix,
    "project_dispatch": project_dispatch,
}
