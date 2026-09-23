"""非 Python 块定位（起止行）+ code_graph 同名消歧的回归护栏。

块起止行的期望值全部是**手工数行**得出的，不是从实现里抄的；
消歧的期望值同样是手工标注（哪一行调用属于哪个定义）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "code_ops"))

_spec = importlib.util.spec_from_file_location(
    "co_span_t", ROOT / "skills" / "code_ops" / "code_ops_impl.py")
co = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(co)

GO = '''package main

import "fmt"

func greet(name string) string {
\ts := "}"
\treturn fmt.Sprintf("hi %s%s", name, s)
}

func main() {
\tfmt.Println(greet("x"))
}

type Server struct {
\tName string
}

func (s *Server) Start(
\taddr string,
) error {
\treturn nil
}
'''

JS = '''// 注释里有个 { 不该被算进块
const obj = {
  a: 1,
  b: 2,
};

function foo(x) {
  const s = "}";
  return x + s;
}

const bar = () => {
  return 1;
};

/* 块注释里的 }
   function fake() { */
function real() {
  return `模板 ${foo("}")}`;
}
'''

RS = '''use std::fmt;

pub fn add(a: i32, b: i32) -> i32 {
    let s = "}";
    a + b
}

pub struct Point {
    x: i32,
}

impl Point {
    pub fn new(x: i32) -> Self {
        Point { x }
    }
}
'''


def _span(root: Path, filename: str, symbol: str) -> str:
    out = co.code_locate({"root": str(root), "symbol": symbol})
    hit = [ln for ln in out.splitlines() if f"{filename}:" in ln]
    assert hit, f"{filename} 里没定位到 {symbol}：\n{out}"
    return hit[0]


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


# ---------- 块起止行：期望值 = 手工数行 ----------

def test_span_go(tmp_path):
    _write(tmp_path, "s.go", GO)
    assert "L5-8" in _span(tmp_path, "s.go", "greet")      # 字符串里的 } 不能截断块
    assert "L10-12" in _span(tmp_path, "s.go", "main")
    assert "L14-16" in _span(tmp_path, "s.go", "Server")
    assert "L18-22" in _span(tmp_path, "s.go", "Start")    # { 在第三行才出现


def test_span_js(tmp_path):
    _write(tmp_path, "s.js", JS)
    assert "L2-5" in _span(tmp_path, "s.js", "obj")
    assert "L7-10" in _span(tmp_path, "s.js", "foo")
    assert "L12-14" in _span(tmp_path, "s.js", "bar")
    # 块注释里的 } 和 function fake() { 都不能干扰真实函数的起止
    assert "L18-20" in _span(tmp_path, "s.js", "real")


def test_span_rust(tmp_path):
    _write(tmp_path, "s.rs", RS)
    assert "L3-6" in _span(tmp_path, "s.rs", "add")
    assert "L8-10" in _span(tmp_path, "s.rs", "Point")
    assert "L13-15" in _span(tmp_path, "s.rs", "new")


def test_span_single_line_declaration_has_no_span(tmp_path):
    # 接口方法这类单行声明没有块，不该硬造一个区间出来
    _write(tmp_path, "i.go", 'package i\n\ntype I interface {\n\tDo() error\n}\n')
    out = co.code_locate({"root": str(tmp_path), "symbol": "Do"})
    assert "i.go:4" in out
    assert "L4-4" not in out and "→" not in out


def test_python_span_comes_from_ast_end_lineno(tmp_path):
    _write(tmp_path, "m.py", "def outer():\n    x = 1\n    return x\n\n\nclass C:\n    pass\n")
    assert "L1-3" in _span(tmp_path, "m.py", "outer")
    assert "L6-7" in _span(tmp_path, "m.py", "C")


# ---------- 同名消歧：期望值 = 手工标注的调用归属 ----------

def test_code_graph_same_name_disambiguation(tmp_path):
    _write(tmp_path, "a.py", "def foo():\n    return 1\n")
    _write(tmp_path, "b.py", "def foo():\n    return 2\n")
    _write(tmp_path, "use_a.py", "from a import foo\n\n\ndef ca():\n    return foo()\n")
    _write(tmp_path, "use_b.py", "from b import foo\n\n\ndef cb():\n    return foo()\n")
    out = co.code_graph({"root": str(tmp_path), "symbol": "foo"})
    assert "同名定义 2 处" in out
    assert "无法归属 0 处" in out
    grp_a = out.split("a.py（L1）")[1].split("2. b.py")[0]
    grp_b = out.split("b.py（L1）")[1]
    assert "use_a.py:5" in grp_a and "use_b.py" not in grp_a
    assert "use_b.py:5" in grp_b and "use_a.py" not in grp_b


def test_code_graph_local_def_wins_over_import(tmp_path):
    _write(tmp_path, "lib.py", "def foo():\n    return 1\n")
    _write(tmp_path, "user.py",
           "from lib import foo\n\n\ndef foo():\n    return 2\n\n\ndef use():\n    return foo()\n")
    out = co.code_graph({"root": str(tmp_path), "symbol": "foo"})
    assert "同名定义 2 处" in out
    grp_user = out.split("user.py（L4）")[1].split("2. lib.py")[0]
    assert "user.py:9" in grp_user          # 本文件定义优先，不归属到 import 的 lib


def test_code_graph_attribute_calls_stay_unattributed(tmp_path):
    # x.foo() 是属性链，AST 图里拿不到来源 → 归到「无法归属」，不猜
    _write(tmp_path, "x.py", "def foo():\n    return 1\n")
    _write(tmp_path, "y.py", "def foo():\n    return 2\n")
    _write(tmp_path, "z.py", "import x\n\n\ndef cz():\n    return x.foo()\n")
    out = co.code_graph({"root": str(tmp_path), "symbol": "foo"})
    assert "无法归属 1 处" in out


# ---------- 死代码保守判定：字符串派发必须算引用 ----------

def test_dead_code_counts_string_dispatch_as_reference(tmp_path):
    _write(tmp_path, "reg.py",
           "def via_subscript():\n    return 1\n\n\n"
           "def via_getattr():\n    return 2\n\n\n"
           "def really_dead():\n    return 3\n")
    _write(tmp_path, "user.py",
           'import reg\n\n\nNS = {}\n\n\ndef go():\n'
           '    f = NS["via_subscript"]\n    return getattr(reg, "via_getattr")\n')
    out = co.code_graph({"root": str(tmp_path)})
    dead = out.split("【疑似死代码】")[1]
    assert "via_subscript" not in dead      # ns["foo"] 是引用
    assert "via_getattr" not in dead        # getattr(x, "foo") 是引用
    assert "really_dead" in dead            # 真死代码仍必须报出来，不能矫枉过正
