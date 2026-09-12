# -*- coding: utf-8 -*-
"""技能/插件热重载自检（被测对象：harness/_reload.py）。

回归背景（两个真实踩坑）：
1. 旧实现只清加载期快照 _mods_added 里的模块，函数体内延迟 import 的兄弟模块
   （skills/android/adb_ui.py 里的 `import viewtree`）永远清不掉 → 改 viewtree.py
   后热重载仍读旧代码（现象：see 报 no attribute res_name）。
2. 改成按 __file__ 归属清除后，若清单里 path 缺失，Path("").resolve() == cwd
   会把整个项目的模块从 sys.modules 摘掉（实测误清 5 个 harness 模块）。
   故清除前必须校验目标目录是 base_dir 的真子目录。

用法：venv/bin/python tools/skills_reload_selftest.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness._reload import evict_dir_modules, is_shared_module  # noqa: E402

_fail = []


def check(ok: bool, label: str) -> None:
    print(("  ✔ " if ok else "  ✘ ") + label)
    if not ok:
        _fail.append(label)


def _load_by_path(p: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _old_evict(entry: dict, skill_dir: Path) -> None:
    """修复前的实现（只清 _mods_added 快照里的模块），用作对照。"""
    root = Path(skill_dir).resolve()
    for mod_name in list(entry.get("_mods_added") or []):
        if mod_name in {"video_lib"}:
            continue
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        p = Path(f).resolve()
        if root == p or root in p.parents:
            sys.modules.pop(mod_name, None)


def t_lazy_import() -> None:
    """延迟 import 的兄弟模块：旧实现漏清，新实现必须清掉。"""
    print("[1] 合成场景：函数体内延迟 import")
    tmp = Path(tempfile.mkdtemp())
    sys.path.insert(0, str(tmp))
    (tmp / "lazy_dep.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (tmp / "entry_mod.py").write_text(
        "def get():\n    import lazy_dep\n    return lazy_dep.VALUE\n", encoding="utf-8")

    def scenario(legacy: bool) -> bool:
        for m in ("entry_mod", "lazy_dep"):
            sys.modules.pop(m, None)
        m = _load_by_path(tmp / "entry_mod.py", "entry_mod")
        # 加载期快照：此刻 lazy_dep 还没进来
        entry = {"_mods_added": ["entry_mod"]}
        m.get()  # 延迟 import 发生在这里
        assert "lazy_dep" in sys.modules, "场景没构造成功"
        if legacy:
            _old_evict(entry, tmp)
        else:
            assert evict_dir_modules(tmp, tmp.parent, label="技能") is True
        return "lazy_dep" in sys.modules

    check(scenario(True) is True, "旧实现残留（复现 bug）")
    check(scenario(False) is False, "新实现清干净（修复生效）")


def t_real_files() -> None:
    """真实文件：adb_ui + viewtree 清缓存后重新 import 必须是新对象。"""
    print("[2] 真实文件：skills/android/adb_ui.py + viewtree.py")
    d = ROOT / "skills" / "android"
    if not (d / "viewtree.py").exists():
        print("  - 跳过（viewtree.py 不存在）")
        return
    sys.path.insert(0, str(d))
    import adb_ui  # noqa: F401
    import viewtree
    old_vt = sys.modules["viewtree"]

    check(evict_dir_modules(d, ROOT / "skills", label="技能") is True, "技能目录通过校验")
    check("viewtree" not in sys.modules, "延迟 import 的 viewtree 已清出 sys.modules")
    check("adb_ui" not in sys.modules, "技能自身模块 adb_ui 已清出")

    import viewtree as vt2
    check(vt2 is not old_vt, "重新 import 拿到新对象（热重载真的生效）")


def t_shared_guard() -> None:
    """共享模块保护：video_lib 与 server 共用实例，绝不能清。"""
    print("[3] 共享模块保护")
    check(is_shared_module("video_lib"), "video_lib 受保护")
    check(is_shared_module("video_lib.util"), "video_lib 子模块受保护")
    check(not is_shared_module("video_libx"), "前缀不误伤 video_libx")


def t_no_collateral() -> None:
    """目录外模块不能被误伤。"""
    print("[4] 目录外模块不被误伤")
    tmp = Path(tempfile.mkdtemp())
    check(evict_dir_modules(tmp, tmp.parent, label="技能") is True, "合法子目录：校验通过")
    check(all(m in sys.modules for m in ("sys", "json", "harness._reload")),
          "sys/json/harness._reload 均保留")


def t_guard() -> None:
    """非法目录一律拒绝：误清项目模块是比漏清严重得多的事故。"""
    print("[5] 非法目录防护")
    before = set(sys.modules)
    check(evict_dir_modules("", ROOT / "skills", label="技能") is False, "空路径：拒绝")
    check(evict_dir_modules(ROOT, ROOT / "skills", label="技能") is False,
          "项目根冒充技能目录（path 缺失时的 cwd）：拒绝")
    check(evict_dir_modules(ROOT / "harness", ROOT / "skills", label="技能") is False,
          "技能目录的兄弟目录：拒绝")
    check(evict_dir_modules(tempfile.mkdtemp(), "", label="技能") is False,
          "基准目录为空：拒绝")
    check(evict_dir_modules(ROOT / "不存在的技能", ROOT / "skills", label="技能") is False,
          "目录不存在：拒绝")
    killed = sorted(m for m in before
                    if m.startswith(("harness.", "tools.")) and m not in sys.modules)
    check(not killed, f"全程未误清项目模块（误清 {len(killed)} 个：{killed[:3]}）")


if __name__ == "__main__":
    t_lazy_import()
    t_real_files()
    t_shared_guard()
    t_no_collateral()
    t_guard()
    print()
    if _fail:
        print(f"FAILED: {len(_fail)} 项 —— " + "; ".join(_fail))
        sys.exit(1)
    print("ALL PASS")
