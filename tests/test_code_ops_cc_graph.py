"""code_ops 圈复杂度口径 + 符号引用图的回归护栏。

复杂度断言对标官方 radon（radon/visitors.py 的 generic_visit / visit_Assert），
锁的是**外部事实**而不是复述实现——实现要改，得先证明 radon 也改了。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "code_ops"))

_spec = importlib.util.spec_from_file_location(
    "co_ccgraph_t", ROOT / "skills" / "code_ops" / "code_ops_impl.py")
co = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(co)


def _cc(src: str) -> dict:
    tree = ast.parse(src)
    return {name: cc for cc, _ln, _e, name, _k in co._py_func_cc(tree)}


SRC = '''
def plain():
    return 1


def branches(x):
    if x: pass
    elif x > 1: pass
    else: pass


def with_stmt():
    with open("x") as f:
        return f


def asserts(a, b):
    assert a and b


def trys():
    try:
        pass
    except ValueError:
        pass
    except KeyError:
        pass
    else:
        pass


def loops(xs):
    for x in xs:
        while x:
            x -= 1


def outer():
    def inner(y):
        if y: pass
        if y: pass
    if 1: pass
'''


def test_cc_matches_radon_rules():
    cc = _cc(SRC)
    assert cc["plain"] == 1
    assert cc["branches"] == 3      # if +1，elif 是嵌套 If 再 +1；else 不加
    assert cc["with_stmt"] == 1     # radon 不计 with（旧实现曾计入，是偏差）
    assert cc["asserts"] == 2       # assert +1 且不递归子节点：a and b 不再计
    assert cc["trys"] == 4          # try：2 个 handler + else = +3
    assert cc["loops"] == 3         # for +1，while +1
    assert cc["outer"] == 2         # 嵌套函数不连坐父函数
    assert cc["inner"] == 3


def test_cc_rank_thresholds():
    # 阈值同 radon/complexity.py 的 cc_rank
    scores = (1, 5, 6, 10, 11, 20, 21, 30, 31, 40, 41)
    assert [co._py_cc_rank(n) for n in scores] == [
        "A", "A", "B", "B", "C", "C", "D", "D", "E", "E", "F"]


def test_code_graph_impact_and_dead_code(tmp_path):
    (tmp_path / "a.py").write_text(
        "def helper():\n    return 1\n\n\ndef unused_pub():\n    return 2\n",
        encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import helper\n\n\ndef caller():\n    return helper()\n",
        encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    assert "被调用 1 处" in out
    dead_part = out.split("【疑似死代码】")[1]
    assert "unused_pub" in dead_part          # 零引用的公开定义进死代码清单
    assert "helper" not in dead_part          # 被调用过的不算死代码


def test_code_graph_symbol_focus(tmp_path):
    (tmp_path / "a.py").write_text(
        "def target():\n    return 1\n\n\ndef user():\n    return target()\n",
        encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path), "symbol": "target"})
    assert "【定义】target" in out
    assert "【被调用】1 处" in out
    assert "in user()" in out


def test_code_graph_skips_test_files_for_dead_code(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def helper_only_for_tests():\n    return 1\n", encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    assert "helper_only_for_tests" not in out.split("【疑似死代码】")[1]


# ---------- 属性调用的口径：谁算「一次调用」 ----------
# 锁的是外部事实：dict.get 不是 Cache.get，re.sub 不是项目里的 sub。
# 这两条都实测踩到过——旧实现把全仓 2103 处 .get( 算给一个同名方法。

def test_code_graph_ignores_unknown_receiver_calls(tmp_path):
    (tmp_path / "a.py").write_text(
        "class Cache:\n"
        "    def get(self, k):\n"
        "        return k\n"
        "\n"
        "\n"
        "def caller(d):\n"
        "    return d.get('x') + d.get('y')\n",
        encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    ranking = out.split("【疑似死代码】")[0]
    assert "被调用 2 处" not in ranking        # d.get() 不能算成 Cache.get 的调用
    assert "另有 1 个定义只被形如 x.f() 的调用引用" in ranking
    assert "get" in ranking.split("另有")[1]


def test_code_graph_ignores_external_module_calls(tmp_path):
    (tmp_path / "a.py").write_text(
        "import re\n"
        "\n"
        "\n"
        "def sub(pattern, repl, s):\n"
        "    return s\n"
        "\n"
        "\n"
        "def caller():\n"
        "    return re.sub('a', 'b', 'c')\n",
        encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    assert "被调用 1 处" not in out.split("【疑似死代码】")[0]


def test_code_graph_counts_module_qualified_project_calls(tmp_path):
    (tmp_path / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "use.py").write_text(
        "import lib\n\n\ndef caller():\n    return lib.helper()\n", encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    assert "被调用 1 处" in out.split("【疑似死代码】")[0]


def test_code_graph_counts_self_method_calls(tmp_path):
    (tmp_path / "a.py").write_text(
        "class A:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def caller(self):\n"
        "        return self.helper()\n",
        encoding="utf-8")
    out = co.code_graph({"root": str(tmp_path)})
    assert "被调用 1 处" in out.split("【疑似死代码】")[0]
