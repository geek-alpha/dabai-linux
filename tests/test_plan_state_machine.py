"""plan_* 工作清单状态机回归测试（对标 codex update_plan 的硬约束）。

覆盖重点是**拒绝路径**：双 in_progress、pending→completed 跳级、非法参数
必须真的被工具层拦住——只测放行路径等于没测。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'skills', 'tasks'))

import plan_impl as P  # noqa: E402


@pytest.fixture
def pl(tmp_path, monkeypatch):
    f = tmp_path / 'agent_plan.json'
    monkeypatch.setattr(P, '_plan_path', lambda: str(f))
    return f


def upd(plan, explanation=None):
    return P._do_update({'plan': plan, 'explanation': explanation})


def test_first_submit_ok(pl):
    r = upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    assert '已更新' in r


def test_reject_two_in_progress(pl):
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    r = upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'in_progress'}])
    assert '只能有 1 个 in_progress' in r


def test_reject_skip_from_pending_to_completed(pl):
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    r = upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    assert '跳到 completed' in r


def test_allow_pending_to_in_progress(pl):
    upd([{'step': 'A', 'status': 'pending'}])
    assert '已更新' in upd([{'step': 'A', 'status': 'in_progress'}])


def test_allow_in_progress_to_completed(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    assert '已更新' in upd([{'step': 'A', 'status': 'completed'}])


def test_retitled_step_is_treated_as_new(pl):
    """计划漂移（改文本）不该被误判成跳级。"""
    upd([{'step': 'A', 'status': 'in_progress'}])
    assert '已更新' in upd([{'step': 'A改', 'status': 'completed'}])


def test_reject_too_many_steps(pl):
    r = upd([{'step': f'S{i}', 'status': 'pending'} for i in range(21)])
    assert '最多 20 条' in r


def test_reject_bad_status(pl):
    assert '非法' in upd([{'step': 'A', 'status': 'doing'}])


def test_reject_empty_step(pl):
    assert '缺 step' in upd([{'step': '   ', 'status': 'pending'}])


def test_reject_overlong_step(pl):
    assert '太长' in upd([{'step': 'A' * 201, 'status': 'pending'}])


def test_reject_non_dict_item(pl):
    assert '不是对象' in upd([{'step': 'A', 'status': 'pending'}, 'nope'])


def test_reject_missing_plan(pl):
    assert '缺少 plan 参数' in upd(None)


def test_reject_plan_not_list(pl):
    assert '必须是数组' in upd('abc')


def test_show_reports_remaining(pl):
    upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'pending'}])
    assert '没完成' in P._do_show({})


def test_show_empty(pl):
    assert '为空' in P._do_show({})


def test_history_keeps_snapshot(pl):
    upd([{'step': 'A', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'in_progress'}])
    assert '快照' in P._do_history({'n': 3})


def test_history_empty(pl):
    assert '还没有历史快照' in P._do_history({})


def test_clear_warns_unfinished(pl):
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    r = P._do_clear({})
    assert '没完成就清了' in r
    assert '为空' in P._do_show({})


def test_clear_when_already_empty(pl):
    assert '本来就是空的' in P._do_clear({})


def test_path_falls_back_to_skill_dir(monkeypatch):
    """user_store 不可用（或匿名）时必须降级到技能目录，而不是抛异常。"""
    monkeypatch.setattr(P, '_plan_path', P.__dict__['_plan_path'])
    assert P._plan_path().endswith('agent_plan.json')
