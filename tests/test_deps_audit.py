"""依赖清单必须与代码事实一致 —— 防止新增 import 后清单分叉。

为什么要有这些测试：清单以前是手写的，dabai.sh 与 dabai.bat 各内联 5 个包，
uvloop / httptools / python-multipart / websockets 全在盲区 —— 装完判定「齐全」，
问题拖到启动时才炸。改成 check_deps.py 单一来源后，清单本身仍是手写的：
新增一个 import，没人会记得回头补清单，同一个坑会再长回来。

判据来自 tools/deps_audit.py（AST 扫 import + server.py 可达性），不是又一份手写清单。

反向也测：清单声明的「启动必需」必须能在启动路径上找到，否则是清单虚高 ——
每个多余的声明都会让用户多装一个包，还会在缺包时报出不存在的问题。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "tools" / "deps_audit.py"
CHECK_DEPS = ROOT / "tools" / "check_deps.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


@pytest.fixture(scope="module")
def report() -> dict:
    proc = subprocess.run([sys.executable, str(AUDIT), "--json"],
                          capture_output=True, text=True, cwd=ROOT, timeout=600)
    assert proc.returncode == 0, f"deps_audit --json 失败：{proc.stderr}"
    return json.loads(proc.stdout)


def test_startup_scan_is_not_empty(report):
    """启动路径不能扫出 0 个依赖 —— 那说明解析器坏了，其余断言会假装通过。

    假绿灯比没有检查更坏：解析器一挂，所有「都在清单里」的结论全部无效。
    """
    assert len(report["startup"]) >= 10, (
        f"启动路径只扫到 {len(report['startup'])} 个依赖，import 图解析可能失效："
        f"{[r['module'] for r in report['startup']]}"
    )
    assert report["entry"] == "server.py"


def test_startup_hard_deps_all_declared(report):
    """启动路径上的硬依赖必须全在清单里 —— 漏一个，新机器装完 server 起不来。"""
    bad = report["startup_hard_missing_decl"]
    assert not bad, (
        "启动路径硬依赖没进清单：" +
        "；".join(f"{r['package'] or r['module']} ← {r['lines'][0]}" for r in bad)
    )


def test_startup_hard_deps_all_installed(report):
    """启动路径上的硬依赖必须都装好 —— 否则本机现在就是坏的。"""
    bad = report["startup_not_installed"]
    assert not bad, (
        "启动路径缺包：" +
        "；".join(f"{r['package'] or r['module']} ← {r['lines'][0]}" for r in bad)
    )


def test_required_items_are_on_startup_path(report):
    """check_deps 的「启动必需」每项都要能在启动路径上找到，或属已知间接依赖。

    这条防的是清单虚高：声明了代码根本不 import 的包，用户白装，缺包时还报出假问题。
    """
    mod = _load(CHECK_DEPS, "_check_deps_under_test")
    indirect = _load(AUDIT, "_deps_audit_under_test").INDIRECT
    on_path = {r["module"] for r in report["startup"]}
    declared_pkgs = {_norm(pkg) for _m, pkg, _e in mod.REQUIRED}
    justified = set()
    for r in report["startup"]:
        if r["package"]:
            justified.add(_norm(r["package"]))
    unjustified = [
        pkg for _m, pkg, _e in mod.REQUIRED
        if pkg not in indirect and _norm(pkg) not in justified
    ]
    assert not unjustified, (
        "清单把非启动路径的包声明成「启动必需」（虚高）：" +
        ", ".join(unjustified) + f"；启动路径上实际是 {sorted(on_path)}"
    )
    assert declared_pkgs, "REQUIRED 空了？"


def test_indirect_entries_carry_evidence():
    """间接依赖表每条都要有证据 —— 填不出证据的，就是真冗余，不该留在清单里。"""
    mod = _load(AUDIT, "_deps_audit_indirect")
    empty = [k for k, v in mod.INDIRECT.items() if not v.strip()]
    assert not empty, f"这些间接依赖没写证据：{empty}"


def test_gate_passes():
    """--gate 退出 0：可挂 CI 的单一判据。"""
    proc = subprocess.run([sys.executable, str(AUDIT), "--gate"],
                          capture_output=True, text=True, cwd=ROOT, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_export_covers_startup_packages(report):
    """一键导出必须包含全部启动路径依赖 —— 导出的清单拿去装新机器要够用。"""
    proc = subprocess.run([sys.executable, str(AUDIT), "--export"],
                          capture_output=True, text=True, cwd=ROOT, timeout=600)
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout.lower()
    missing = [r["package"] for r in report["startup"]
               if r["package"] and not r["optional"] and r["package"].lower() not in text]
    assert not missing, f"--export 漏了启动路径依赖：{missing}"


def test_noise_dirs_excluded_from_scan(report):
    """vendored 第三方代码不算大白的依赖 —— vendor_ots 的 Cryptodome / imp 不该出现。

    误判的代价是双向的：报出来逼人往清单里塞不存在的包，或者干脆把清单搞成噪音墙。
    """
    mods = {r["module"] for r in report["not_on_pypi"]} | \
           {r["module"] for r in report["missing_declaration"]}
    for noise in ("Cryptodome", "otsclient", "gitdb", "pexpect", "pkg_resources"):
        assert noise not in mods, f"{noise} 来自 vendored 代码/依赖缓存，不该进依赖报告"


# ---------- 扫描范围：单一来源 + 分口径 ----------

BUILD = ROOT / "deploy" / "release" / "build_release.py"
SCOPE = ROOT / "tools" / "scan_scope.py"


def _load_code_ops():
    skill = ROOT / "skills" / "code_ops"
    if str(skill) not in sys.path:
        sys.path.insert(0, str(skill))
    return _load(skill / "code_ops_impl.py", "code_ops_impl_under_test")


def test_scan_scope_is_single_source():
    """两份同类名单必须出自同一处。

    tools/deps_audit.py 的 SKIP_DIRS 和 code_ops 的 NOISE_DIRS 曾各写一份，
    一边排了另一边没排，tools/vendor_ots 的 gitdb/Cryptodome 就混进了影响面 Top12。
    """
    scope = _load(SCOPE, "scan_scope_under_test")
    audit = _load(AUDIT, "deps_audit_under_test")
    code_ops = _load_code_ops()

    assert audit.SKIP_DIRS == (scope.COMMON_DIRS | scope.DEPS_EXTRA)
    assert code_ops._scan_scope is not None, (
        "code_ops 退回了内置兜底名单 —— 项目内必须走 tools/scan_scope.py"
    )
    assert set(code_ops.NOISE_DIRS) == (scope.COMMON_DIRS | scope.CODE_EXTRA)
    assert code_ops.NOISE_PREFIXES == scope.NOISE_PREFIXES


def test_scope_split_keeps_code_search_wide():
    """口径差异是有意的：依赖扫描排 data/，代码搜索不排。

    合成一份名单会误伤 —— 用户把自己的脚本放在 data/ 下，就再也搜不到了。
    """
    scope = _load(SCOPE, "scan_scope_under_test")
    for only_deps in ("data", "models", "backgrounds", "logs"):
        assert scope.is_dep_scan_noise(only_deps), f"{only_deps} 该在依赖扫描里被排"
        assert not scope.is_code_scan_noise(only_deps), f"{only_deps} 不该在代码搜索里被排"
    # 命名变体：vendor_ots / third_party_x 两边都排，myvendor 两边都不误伤
    for probe in ("vendor_ots", "third_party_x", ".pytest_libs_old"):
        assert scope.is_dep_scan_noise(probe) and scope.is_code_scan_noise(probe), probe
    assert not scope.is_dep_scan_noise("myvendor")
    assert not scope.is_code_scan_noise("src")


# ---------- 打包前的依赖闸门 ----------

def _load_build():
    return _load(BUILD, "build_release_under_test")


def _fake_root(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "deps_audit.py").write_text(body, encoding="utf-8")
    return root


def test_deps_gate_blocks_on_undeclared_startup_dep(tmp_path):
    """闸门要真拦得住：漏网依赖 = 包本身有缺陷，宁可不出包。"""
    build = _load_build()
    root = _fake_root(tmp_path, (
        "import json\n"
        "print(json.dumps({'startup_hard_missing_decl': [\n"
        "    {'package': 'fake-pkg', 'module': 'fakepkg', 'lines': ['server.py:7']}]}))\n"
    ))
    gaps, note = build.deps_gate(root)
    assert not note, note
    assert len(gaps) == 1 and "fake-pkg" in gaps[0] and "server.py:7" in gaps[0]


def test_deps_gate_passes_on_this_repo():
    """本仓打包必须过闸门 —— 这是 requirements-core.txt 与代码一致的现场证据。"""
    gaps, note = _load_build().deps_gate(ROOT)
    assert not note, f"依赖闸门没真跑成（不是通过）：{note}"
    assert not gaps, f"启动路径有未声明依赖：{gaps}"


def test_deps_gate_is_loud_when_it_cannot_run(tmp_path):
    """跑不成时必须出声 —— 静默放行等于没有闸门。"""
    build = _load_build()
    empty = tmp_path / "empty"
    empty.mkdir()
    gaps, note = build.deps_gate(empty)
    assert not gaps and note, "找不到 tools/deps_audit.py 却没出声，闸门形同虚设"

    gaps2, note2 = build.deps_gate(_fake_root(tmp_path, "print('不是 JSON')\n"))
    assert not gaps2 and note2, "输出不是 JSON 却静默放行"


def test_deps_gate_is_wired_into_main():
    """闸门必须接在打包主流程上 —— 定义了没人调，是假绿灯的另一种写法。"""
    import ast
    tree = ast.parse(BUILD.read_text(encoding="utf-8"))
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {n.func.id for n in ast.walk(main)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "deps_gate" in called, "main() 里没调用 deps_gate —— 闸门没接上"


def test_deps_gate_reads_real_audit_output(tmp_path):
    """真 deps_audit + 真 build_release 串起来跑：两边 JSON 契约必须对得上。

    上面那条用的是假脚本，只证明解析逻辑；这条证明「有缺口时真能读出来」——
    rep.get("startup_hard_missing_decl") 的键名一旦对不上，缺口就静默变成空列表。
    """
    import shutil
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    for name in ("deps_audit.py", "check_deps.py", "scan_scope.py"):
        shutil.copy(ROOT / "tools" / name, root / "tools" / name)
    (root / "server.py").write_text("import zzz_fake_pkg\n", encoding="utf-8")

    gaps, note = _load_build().deps_gate(root)
    assert not note, note
    assert any("zzz_fake_pkg" in g for g in gaps), (
        f"临时仓库 import 了未声明的包，闸门却没报出来：{gaps}"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
