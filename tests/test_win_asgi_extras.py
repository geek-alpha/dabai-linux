# -*- coding: utf-8 -*-
"""Windows 启动守卫：ASGI 加速项（uvloop / httptools）必须按可用性给，不能硬编码。

uvloop 在 PyPI 上没有 Windows wheel（0.22.1 的 49 个文件里 0 个 win），httptools
要 C 编译。硬编码 loop="uvloop" 的后果是 Windows 上 server 启动即崩 —— 失败点在
uvicorn.Config 构造里，日志都来不及写，看起来像"脚本没反应"。

这里用 AST 抽出 _asgi_fast_extras 单独 exec，不 import server（那会拉起 FastAPI app
与整个 harness）。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def _load_helper():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_asgi_fast_extras":
            ns: dict = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER), "exec"), ns)
            return ns["_asgi_fast_extras"]
    raise AssertionError("server.py 里找不到 _asgi_fast_extras（契约变了，需重审）")


def _hardcoded_extras():
    """AST 级检查：任何 loop="uvloop" / http="httptools" 的关键字实参都算硬编码。

    按关键字节点判，不按文本行判 —— 否则注释和文档字符串里提到这两个名字也会误报。
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in ("loop", "http"):
            if getattr(node.value, "value", None) in ("uvloop", "httptools"):
                hits.append(f"server.py:{node.value.lineno} {node.arg}={node.value.value!r}")
    return hits


def test_no_hardcoded_asgi_backend():
    hits = _hardcoded_extras()
    assert not hits, f"硬编码加速后端会在装不上它们的机器上崩在启动：{hits}"


def test_extras_passed_when_available():
    """本机（Linux venv）两个加速项都在 → 参数照给，不许白白降级。"""
    extras = _load_helper()()
    assert extras.get("loop") == "uvloop", extras
    assert extras.get("http") == "httptools", extras


def test_extras_skipped_when_missing(monkeypatch):
    """sys.modules 里置 None → import 抛 ImportError，模拟 Windows 上装不上。"""
    for name in ("uvloop", "httptools"):
        monkeypatch.setitem(sys.modules, name, None)
    extras = _load_helper()()
    assert extras == {}, f"缺了加速项还硬给 uvicorn 参数：{extras}"
