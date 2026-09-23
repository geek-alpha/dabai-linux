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


def _load_selfcheck():
    spec = importlib.util.spec_from_file_location("_selfcheck_under_test", SELFCHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_selfcheck_exists_and_shim_kept():
    assert SELFCHECK.is_file(), "tools/selfcheck.py 不见了"
    assert SHIM.is_file(), "旧路径兼容壳被删了 —— 老脚本引用会断"


def test_required_packages_cover_every_non_comment_line():
    """requirements-core.txt 里每个非注释行都要被解析进清单。"""
    mod = _load_selfcheck()
    parsed = mod._required_packages()
    expected = [ln.strip() for ln in REQS.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    assert len(parsed) == len(expected), (
        f"清单解析到 {len(parsed)} 个，文件里有 {len(expected)} 个非注释行")


def test_required_packages_include_previously_missing_ones():
    """这几个真被代码用到、却在旧硬编码清单之外（qrcode 缺了扫码页直接 500）。"""
    mod = _load_selfcheck()
    parsed = mod._required_packages()
    keys = {k.lower() for k in parsed}
    for name in ("qrcode", "anthropic", "httpx", "mss", "netifaces", "yt_dlp"):
        assert name in keys, f"requirements-core.txt 里没有 {name}，或解析漏了"


def test_pypi_to_import_name_mapping():
    """Pillow 的 import 名是 PIL、python-dotenv 是 dotenv —— 映射错了会误报缺包。"""
    mod = _load_selfcheck()
    parsed = mod._required_packages()
    by_key = {k.lower(): v for k, v in parsed.items()}
    assert by_key.get("pillow") == "PIL"
    assert by_key.get("python-dotenv") == "dotenv"
    assert by_key.get("qrcode") == "qrcode"


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
