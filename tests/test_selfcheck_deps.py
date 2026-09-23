"""自检的依赖清单必须和 requirements-core.txt 同源。

硬编码清单会跟安装清单分叉：`--setup` 装完仍缺包，自检却报 [ OK ]。
假绿灯比没有自检更坏 —— 这条测试就是防它再长回来。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SELFCHECK = ROOT / "tools" / "selfcheck.py"
SHIM = ROOT / "tools" / "linux_selfcheck.py"
REQS = ROOT / "requirements-core.txt"
CHECK_DEPS = ROOT / "tools" / "check_deps.py"


def _load_selfcheck():
    spec = importlib.util.spec_from_file_location("_selfcheck_under_test", SELFCHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_check_deps():
    spec = importlib.util.spec_from_file_location("_check_deps_under_test", CHECK_DEPS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(name: str) -> str:
    """PEP 503 规范化：- / _ / . 在包名里等价（yt-dlp == yt_dlp）。"""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _declared_packages(mod) -> set[str]:
    """check_deps.py 声明的全部包名（规范化小写）：启动必需 + 能力依赖。"""
    rows = list(mod.REQUIRED) + list(mod.CAPABILITY)
    return {_norm(pkg) for _, pkg, *_ in rows}


def _req_lines() -> list[str]:
    """requirements-core.txt 的非注释行（去掉行内注释与首尾空白）。"""
    out = []
    for raw in REQS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_selfcheck_exists_and_shim_kept():
    assert SELFCHECK.is_file(), "tools/selfcheck.py 不见了"
    assert SHIM.is_file(), "旧路径兼容壳被删了 —— 老脚本引用会断"


def test_required_packages_cover_every_non_comment_line():
    """requirements-core.txt 里每个非注释行都要出现在 check_deps 的清单里。

    清单硬编码在 tools/check_deps.py（单一来源），requirements-core.txt 是安装清单 ——
    两者分叉的后果是：--setup 装完仍缺包，而自检报 [ OK ]。
    """
    mod = _load_check_deps()
    declared = _declared_packages(mod)
    missing = [line for line in _req_lines()
               if _norm(line.split(";", 1)[0]) not in declared]
    assert not missing, (
        "requirements-core.txt 里这些包不在 check_deps.py 清单里，"
        "--setup 装完仍会被判「缺依赖」：\n  " + "\n  ".join(missing))


def test_required_packages_include_previously_missing_ones():
    """这几个真被代码用到、却在旧硬编码清单之外（qrcode 缺了扫码页直接 500）。

    不含 anthropic / python-dotenv：全仓 `import anthropic` 与 `load_dotenv` 零命中
    （它们是早期 requirements.txt 抄过来的，证据见 requirements-core.txt 文末可选段）。
    清单里多一个没用的包，用户就多装一个，缺包时还会报出不存在的问题。
    """
    mod = _load_check_deps()
    declared = _declared_packages(mod)
    for name in ("qrcode", "httpx", "mss", "netifaces", "yt-dlp"):
        assert name in declared, f"check_deps.py 清单里没有 {name}"


def test_pypi_to_import_name_mapping():
    """import 名 ≠ 包名的那几个必须映射对 —— 映射错了会误报缺包或漏报真缺的包。

    Pillow→PIL、python-multipart→multipart、yt-dlp→yt_dlp。
    """
    mod = _load_check_deps()
    pairs = {pkg.lower(): mod_name for mod_name, pkg, *_ in
             list(mod.REQUIRED) + list(mod.CAPABILITY)}
    assert pairs.get("pillow") == "PIL"
    assert pairs.get("python-multipart") == "multipart"
    assert pairs.get("yt-dlp") == "yt_dlp"
    assert pairs.get("qrcode") == "qrcode"
    assert pairs.get("edge-tts") == "edge_tts"


def test_selfcheck_runs_and_emits_valid_json():
    proc = subprocess.run([sys.executable, str(SELFCHECK), "--json"],
                          capture_output=True, text=True, cwd=ROOT, timeout=180)
    data = json.loads(proc.stdout)
    assert isinstance(data, list) and data
    dep = [row for row in data if row["item"] == "核心依赖"]
    assert dep, "报告里没有「核心依赖」这一项"
    # 清单已同源，本机装齐了就不该再报缺少
    assert dep[0]["level"] in ("OK", "FAIL"), dep[0]
    assert "requirements-core.txt" not in dep[0]["detail"] or dep[0]["level"] == "WARN"


def test_shim_forwards_to_new_script():
    proc = subprocess.run([sys.executable, str(SHIM), "--json"],
                          capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert "已改名" in proc.stderr
    assert isinstance(json.loads(proc.stdout), list)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
