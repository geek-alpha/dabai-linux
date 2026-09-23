#!/usr/bin/env python3
"""依赖审计 —— 从代码实际 import 反推真实依赖，与清单对账。

为什么需要它：人肉维护的清单必然与代码分叉。以前 dabai.sh 与 dabai.bat 各内联
5 个包，uvloop / httptools / python-multipart / websockets 全在盲区；换成
check_deps.py 单一来源后，清单本身仍是手写的 —— 新增一个 import 没人会记得
回头补清单，缺包就变成运行到某个接口才炸的「不完整报错」。

本工具把方向倒过来：以代码为事实来源（AST 扫 import，含 importlib.import_module
字符串形式），用 importlib.metadata.packages_distributions() 做
「顶层模块名 → 发行包名」的权威映射（不手写对照表），再和 check_deps 的清单对账。

★ 启动路径分析：从 server.py 出发沿 import 图递归，切出「缺了 server 就起不来」的
那批依赖。全项目扫描会把一次性工具脚本的依赖（tools/locate_icon.py 的 cv2、
screen_shot.py 的 pyautogui）和历史脚本（dabai.py 依赖的本机私有包）一并算进来，
按那个清单装是过度安装。启动路径才是「完整」的准确口径。

判定分四类：
  [漏网]  启动路径用了、清单没声明 —— 新机器装完仍缺，最危险
  [未装]  代码用了、本机环境也没有 —— 现在就是坏的
  [冗余]  清单声明了、代码里扫不到 —— 可能是动态加载或历史遗留
  [可选]  import 被 try/except 包住 = 有降级路径，不该塞进启动必需

用法：
    python3 tools/deps_audit.py              # 人类可读报告
    python3 tools/deps_audit.py --startup    # 只看启动路径
    python3 tools/deps_audit.py --json       # 机器可读
    python3 tools/deps_audit.py --gate       # 启动路径有漏网/未装 → 退出 1
    python3 tools/deps_audit.py --root /path/to/repo   # 审别的检出目录（默认本仓）
    python3 tools/deps_audit.py --export     # 打印直接依赖清单（带本机版本）
    python3 tools/deps_audit.py --export -o requirements-direct.txt
    python3 tools/deps_audit.py --freeze -o requirements-lock.txt   # pip freeze 全量
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import json
import subprocess
import sys
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_deps  # noqa: E402
import scan_scope  # noqa: E402

ENTRY = "server.py"

# 不扫的目录：名单来自 tools/scan_scope.py —— 与 code_ops 同一来源。
# 以前两处各写一份同类名单（这里 SKIP_DIRS、那边 NOISE_DIRS），一边排了另一边没排，
# vendored 第三方符号就混进报告。口径差异也摆在那里：依赖扫描多排 data/（轮快照里的
# 代码副本）与 models/（二进制资源），代码搜索不排——用户脚本可能就在那些目录里。
SKIP_DIRS = scan_scope.COMMON_DIRS | scan_scope.DEPS_EXTRA

# 非 PyPI 包：Blender 内嵌 Python（bpy/mathutils）与本机私有模块（dabai_*）。
# 它们不会也不该出现在 requirements 里 —— 列出来只是为了让报告不把它们当「漏网」。
NOT_ON_PYPI = {
    "bpy", "bmesh", "mathutils", "addon_utils", "rna_prop_ui",
    "amazing_agent_dingding", "dabai_ears", "dabai_voice",
    "fuctions_all_you_need_base",
}

# 清单里有、但代码里没有静态 import 的包：运行时按名加载，删了会崩。
# 每条必须带证据；填不出的就是真冗余，报告里单列。
INDIRECT = {
    "uvloop": "server.py:8517 importlib.import_module 按名加载（uvicorn loop 参数）",
    "httptools": "server.py:8517 importlib.import_module 按名加载（uvicorn http 参数）",
    "python-multipart": "FastAPI 加载 File/UploadFile 时按名需要（server.py:41）",
}

# 捕获这些异常都算「有降级路径」：except Exception 同样能吞掉 ImportError。
_DOWNGRADE_EXC = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


class _ImportScan(ast.NodeVisitor):
    """收集 (模块名, level, 行号, 是否被 try 保护)。level>0 是相对导入。"""

    def __init__(self) -> None:
        self.hits: list[tuple[str, int, int, bool]] = []
        self._guarded: list[bool] = []

    def _optional(self) -> bool:
        return any(self._guarded)

    def visit_Try(self, node: ast.Try) -> None:
        catches = any(
            (isinstance(h.type, ast.Name) and h.type.id in _DOWNGRADE_EXC)
            or (isinstance(h.type, ast.Tuple) and any(
                isinstance(e, ast.Name) and e.id in _DOWNGRADE_EXC for e in h.type.elts))
            or h.type is None  # 裸 except
            for h in node.handlers
        )
        self._guarded.append(catches)
        for stmt in node.body:
            self.visit(stmt)
        self._guarded.pop()
        for h in node.handlers:
            self.visit(h)
        for s in node.orelse:
            self.visit(s)
        for s in node.finalbody:
            self.visit(s)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.hits.append((a.name, 0, node.lineno, self._optional()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # 拼全名再解析：`from tools import plan_stall` 的裸 module 是命名空间包 tools
        # （tools/ 无 __init__.py），按裸名解析会落到「项目内找不到」→ 误判成第三方依赖。
        if node.module or node.level:
            names = [a.name for a in node.names if a.name != "*"]
            if node.module and names:
                for n in names:
                    self.hits.append((f"{node.module}.{n}", node.level, node.lineno, self._optional()))
            else:
                self.hits.append((node.module or "", node.level, node.lineno, self._optional()))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        is_dyn = (isinstance(fn, ast.Attribute) and fn.attr == "import_module") \
            or (isinstance(fn, ast.Name) and fn.id == "__import__")
        if is_dyn and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            self.hits.append((node.args[0].value, 0, node.lineno, self._optional()))
        self.generic_visit(node)


def _skip_path(p: Path) -> bool:
    return any(scan_scope.is_dep_scan_noise(part) for part in p.relative_to(ROOT).parts)


def _py_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        if _skip_path(p):
            continue
        out.append(p)
    return out


@lru_cache(maxsize=1)
def _stem_index() -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = defaultdict(list)
    for p in _py_files():
        idx[p.stem].append(p)
    return dict(idx)


def _module_index() -> dict[str, Path]:
    """点分模块名 → 文件（模拟 sys.path[0]=项目根 的解析）。"""
    idx: dict[str, Path] = {}
    for p in _py_files():
        rel = p.relative_to(ROOT)
        if p.name == "__init__.py":
            idx[".".join(rel.parent.parts)] = p
        else:
            idx[".".join(rel.with_suffix("").parts)] = p
    return idx


def _resolve(mod: str, level: int, cur: Path, idx: dict[str, Path]) -> Path | None:
    """把一个 import 语句解析成项目内文件；解析不到 → None（第三方）。"""
    if level:
        base = cur.parent
        for _ in range(level - 1):
            base = base.parent
        try:
            parts = base.relative_to(ROOT).parts
        except ValueError:
            return None
        name = ".".join(parts + tuple(mod.split(".")) if mod else parts)
    else:
        name = mod
    if name in idx:
        return idx[name]
    # 逐级回退：from tools.foo.bar import x，tools.foo.bar 可能是文件也可能上层
    while "." in name:
        name = name.rsplit(".", 1)[0]
        if name in idx:
            return idx[name]
    # 同目录兜底：skills/appearance/skill.py 里的 `import appearance_impl`
    # 只有在 skills/appearance 被加进 sys.path 时才成立，但仍然是项目内文件。
    top = (mod.split(".")[0] if mod else "")
    if top:
        cand = cur.parent / f"{top}.py"
        if cand.is_file():
            return cand
        cand = cur.parent / top / "__init__.py"
        if cand.is_file():
            return cand
    # 全项目按文件名兜底：server.py:8026 把 skills/media 插进 sys.path 后才
    # `import video_lib`，tools/plan_view.py:42 对 skills/tasks 同理。
    # 只认唯一命中 —— 重名时无法判断是哪个，宁可报出来让人看。
    cands = _stem_index().get(top, [])
    if len(cands) == 1:
        return cands[0]
    return None


def _walk(start_files: list[Path]) -> tuple[set[Path], dict[str, dict]]:
    """BFS 沿项目内 import 图走，返回 (可达文件集, 第三方模块表)。"""
    idx = _module_index()
    seen: set[Path] = set()
    third: dict[str, dict] = defaultdict(lambda: {"files": [], "lines": [], "guarded": True})
    stack = [p for p in start_files if p.is_file()]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        v = _ImportScan()
        v.visit(tree)
        rel = str(path.relative_to(ROOT))
        for mod, level, lineno, guarded in v.hits:
            if not mod:
                continue
            target = _resolve(mod, level, path, idx)
            if target is not None:
                stack.append(target)
                continue
            top = mod.split(".")[0]
            if top.startswith("_") or top in sys.stdlib_module_names:
                continue
            third[top]["files"].append(rel)
            third[top]["lines"].append(f"{rel}:{lineno}")
            if not guarded:
                third[top]["guarded"] = False
    return seen, third


def scan_all() -> dict[str, dict]:
    """全项目扫描（含一次性工具脚本）—— 用于找「代码用了但清单没有」。"""
    local = {p.stem for p in _py_files()}
    local |= {p.name for p in ROOT.rglob("*")
              if p.is_dir() and p.name.isidentifier() and not _skip_path(p)}
    found: dict[str, dict] = defaultdict(lambda: {"files": set(), "guarded": True, "lines": []})
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        v = _ImportScan()
        v.visit(tree)
        rel = str(path.relative_to(ROOT))
        for mod, level, lineno, guarded in v.hits:
            if not mod or level:
                continue
            top = mod.split(".")[0]
            if top in local or top.startswith("_") or top in sys.stdlib_module_names:
                continue
            found[top]["files"].add(rel)
            found[top]["lines"].append(f"{rel}:{lineno}")
            if not guarded:
                found[top]["guarded"] = False
    return found


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _declared() -> dict[str, str]:
    out = {}
    for _mod, pkg, _ex in check_deps.REQUIRED:
        out[_norm(pkg)] = "启动必需"
    for _mod, pkg, _what, _ex in check_deps.CAPABILITY:
        out[_norm(pkg)] = "能力依赖"
    return out


def _row(mod: str, info: dict, dmap: dict, declared: dict, startup: bool) -> dict:
    dists = dmap.get(mod)
    dist = dists[0] if dists else None
    files = sorted(info["files"]) if isinstance(info["files"], set) else list(info["files"])
    return {
        "module": mod,
        "package": dist,
        "installed": dist is not None,
        "optional": info["guarded"],
        "declared": _norm(dist) in declared if dist else False,
        "on_startup": startup,
        "not_on_pypi": mod in NOT_ON_PYPI,
        "lines": info["lines"][:5],
        "files": files[:5],
    }


def audit() -> dict:
    dmap = md.packages_distributions()
    declared = _declared()

    _seen, startup_third = _walk([ROOT / ENTRY])
    startup_rows = [_row(m, i, dmap, declared, True) for m, i in sorted(startup_third.items())]
    startup_mods = set(startup_third)

    all_found = scan_all()
    rows = [_row(m, i, dmap, declared, m in startup_mods) for m, i in sorted(all_found.items())]

    seen_dists = {_norm(r["package"]) for r in rows if r["package"]}
    redundant = [{"package": p, "kind": k} for p, k in sorted(declared.items())
                 if p not in seen_dists]
    return {
        "entry": ENTRY,
        "declared_count": len(declared),
        "startup": startup_rows,
        "startup_hard_missing_decl": [r for r in startup_rows
                                      if not r["declared"] and not r["optional"]
                                      and not r["not_on_pypi"]],
        "startup_not_installed": [r for r in startup_rows
                                  if not r["installed"] and not r["optional"]
                                  and not r["not_on_pypi"]],
        "missing_declaration": [r for r in rows if not r["declared"]],
        "not_installed": [r for r in rows if not r["installed"] and not r["not_on_pypi"]],
        "not_on_pypi": [r for r in rows if r["not_on_pypi"]],
        "redundant": redundant,
        "indirect": [r for r in redundant if r["package"] in INDIRECT],
        "zero_ref": [r for r in redundant if r["package"] not in INDIRECT],
        "scanned": len(rows),
    }


def _pkg_line(dist: str) -> tuple[str, bool]:
    try:
        return f"{dist}=={md.version(dist)}", True
    except md.PackageNotFoundError:
        return dist, False


def _export_direct() -> list[str]:
    """直接依赖 = 代码真正 import 的包（不含传递依赖），带本机版本。"""
    dmap = md.packages_distributions()
    declared = _declared()
    found = scan_all()
    _seen, startup_third = _walk([ROOT / ENTRY])
    rows = []
    for mod, info in found.items():
        dists = dmap.get(mod)
        dist = dists[0] if dists else mod
        rows.append((_norm(dist), dist, info["guarded"], mod in startup_third,
                     mod in NOT_ON_PYPI))
    rows.sort()
    lines = [
        f"# 大白直接依赖（代码 import 反推）— tools/deps_audit.py --export，{date.today()}",
        "# 只含代码真正 import 的包，不含传递依赖；版本为生成本机的实际安装版本。",
        "# 完整环境复现用 --freeze（pip freeze 全量锁定）。",
        "",
    ]
    groups = (
        ("启动路径 · 启动必需", lambda r: r[3] and declared.get(r[0]) == "启动必需"),
        ("启动路径 · 能力依赖", lambda r: r[3] and declared.get(r[0]) == "能力依赖"),
        ("启动路径 · 未归类", lambda r: r[3] and declared.get(r[0]) not in ("启动必需", "能力依赖")),
        ("非启动路径（工具脚本 / 可选能力）", lambda r: not r[3]),
    )
    for title, pred in groups:
        group = [r for r in rows if pred(r)]
        if not group:
            continue
        lines.append(f"# ---- {title} ----")
        for _n, dist, guarded, _s, nonpypi in group:
            text, ok = _pkg_line(dist)
            if nonpypi:
                text += "   # 非 PyPI 包（本机私有 / 宿主内嵌），装不上属正常"
            elif guarded:
                text += "   # 可选：import 有降级路径"
            elif not ok:
                text += "   # 未安装"
            lines.append(text)
        lines.append("")
    return lines


def _write(lines: list[str], args: list[str]) -> int:
    if "-o" in args:
        out = Path(args[args.index("-o") + 1])
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已写出 {out}（{len(lines)} 行）")
    else:
        print("\n".join(lines))
    return 0


def main() -> int:
    args = sys.argv[1:]

    # --root：打包器在别的检出目录里跑（工作树），审计要跟着走。
    # ROOT 是模块级全局，函数内部按全局查找 —— 覆盖它就够了。
    if "--root" in args:
        target = Path(args[args.index("--root") + 1]).resolve()
        if not (target / ENTRY).is_file():
            print(f"✘ {target} 不像仓库根：找不到 {ENTRY}", file=sys.stderr)
            return 2
        globals()["ROOT"] = target

    if "--export" in args:
        return _write(_export_direct(), args)
    if "--freeze" in args:
        proc = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"pip freeze 失败：{proc.stderr.strip()}", file=sys.stderr)
            return 1
        head = [f"# 大白完整环境锁定（pip freeze 全量，含传递依赖）— {date.today()}",
                "# 用途：复现同版本环境。跨平台换机用 requirements-core.txt。", ""]
        return _write(head + proc.stdout.strip().splitlines(), args)

    rep = audit()
    if "--json" in args:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    st = rep["startup"]
    print(f"启动路径（{rep['entry']} 可达）：{len(st)} 个第三方依赖"
          f"；全项目扫描：{rep['scanned']} 个；清单声明：{rep['declared_count']} 个包")

    bad = 0
    if rep["startup_not_installed"]:
        bad += len(rep["startup_not_installed"])
        print(f"\n[X] 启动路径上缺包（{len(rep['startup_not_installed'])}）—— 现在 server 就起不来：")
        for r in rep["startup_not_installed"]:
            print(f"      {r['package'] or r['module']}  ← {r['lines'][0]}")
    if rep["startup_hard_missing_decl"]:
        bad += len(rep["startup_hard_missing_decl"])
        print(f"\n[X] 启动路径硬依赖、清单没声明（{len(rep['startup_hard_missing_decl'])}）"
              f"—— 新机器装完 requirements-core.txt 仍缺：")
        for r in rep["startup_hard_missing_decl"]:
            print(f"      {r['package'] or r['module']}  ← {r['lines'][0]}")

    off = [r for r in rep["missing_declaration"] if not r["on_startup"] and not r["optional"]]
    if off:
        print(f"\n[·] 非启动路径的依赖、清单没声明（{len(off)}）—— 不影响启动，工具脚本才用：")
        for r in off:
            tag = "（非 PyPI 包，装不上）" if r["not_on_pypi"] else ""
            print(f"      {r['package'] or r['module']}{tag}  ← {r['lines'][0]}")
    opt = [r for r in rep["missing_declaration"] if r["optional"]]
    if opt:
        print(f"\n[·] 可选依赖、清单没声明（{len(opt)}）—— import 有降级路径：")
        for r in opt:
            tag = "（非 PyPI 包）" if r["not_on_pypi"] else ""
            print(f"      {r['package'] or r['module']}{tag}  ← {r['lines'][0]}")
    if rep["indirect"]:
        print(f"\n[·] 清单里有、代码无静态 import 的间接依赖（{len(rep['indirect'])}）—— 运行时按名加载，不能删：")
        for r in rep["indirect"]:
            print(f"      {r['package']}：{INDIRECT[r['package']]}")
    if rep["zero_ref"]:
        print(f"\n[!] 清单里有、代码零引用（{len(rep['zero_ref'])}）—— 建议人工确认是否还需要：")
        for r in rep["zero_ref"]:
            print(f"      {r['package']}（{r['kind']}）")
    if not bad:
        print(f"\n[ OK ] 启动路径依赖完整：{len(st)} 个第三方依赖全在清单内且已安装")

    if "--gate" in args and bad:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
