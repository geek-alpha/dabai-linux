"""任务中心「工作清单」合成条目回归测试（tools/plan_view.py）。

重点测两条容易做错的：
1. 清单为空必须返回 None —— 否则任务中心常驻一条空条目刷屏；
2. created_at 必须取首次提交时间 —— 用 updated_at 会让清单每步都跳到列表顶部。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'skills', 'tasks'))
sys.path.insert(0, os.path.join(_ROOT, 'tools'))

import plan_impl as P  # noqa: E402
import plan_view as V  # noqa: E402


@pytest.fixture
def pl(tmp_path, monkeypatch):
    f = tmp_path / 'agent_plan.json'
    monkeypatch.setattr(P, '_plan_path', lambda: str(f))
    return f


def upd(plan, explanation=None):
    return P._do_update({'plan': plan, 'explanation': explanation})


def snap(full=False):
    return V.snapshot(full=full)


def test_empty_plan_returns_none(pl):
    """空清单不合成条目：任务中心不该常驻一条空任务。"""
    assert snap() is None


def test_snapshot_after_submit(pl):
    upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'pending'}])
    s = snap()
    assert s['id'] == V.TASK_ID
    assert s['kind'] == 'plan' and s['channel'] == 'plan'
    assert s['status'] == 'running'
    assert s['extra']['plan']['total'] == 2
    assert s['extra']['plan']['done'] == 0


def test_all_completed_marks_done(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    upd([{'step': 'A', 'status': 'completed'}])
    assert snap()['status'] == 'done'


def test_title_carries_progress(pl):
    upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'pending'}])
    assert '0/2' in snap()['title']
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    assert '1/2' in snap()['title']


def test_no_in_progress_is_flagged_in_title(pl):
    """全部 pending 是「忘了标进行中」，标题要显式提示，不能静默。"""
    upd([{'step': 'A', 'status': 'pending'}])
    assert '没有进行中' in snap()['title']


def test_steps_is_focus_digest_not_full_table(pl):
    """steps 只放焦点摘要：全表在 extra.plan，两处都放等于同一屏看两遍。"""
    upd([{'step': f'S{i}', 'status': 'pending'} for i in range(6)])
    upd([{'step': 'S0', 'status': 'in_progress'}] +
        [{'step': f'S{i}', 'status': 'pending'} for i in range(1, 6)])
    s = snap()
    assert len(s['steps']) <= 4
    assert any('进行中' in x for x in s['steps'])
    assert len(s['extra']['plan']['steps']) == 6


def test_created_at_is_first_submit_not_updated(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    first = snap()['created_at']
    upd([{'step': 'A', 'status': 'completed'}])
    s = snap()
    assert s['created_at'] == first, 'created_at 变了会让清单每步都跳到列表顶部'
    assert s['updated_at'] >= s['created_at']


def test_explanation_surfaces(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'pending'}],
        explanation='拆成两步')
    s = snap()
    assert s['extra']['plan']['explanation'] == '拆成两步'
    assert any('拆成两步' in x for x in s['steps'])


def test_full_snapshot_has_brief(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    assert snap(full=True)['brief']
    assert 'brief' not in snap(full=False)


def test_corrupt_file_does_not_raise(pl):
    """坏 JSON 不能让任务中心列表整体挂掉。"""
    pl.write_text('{ 这不是 json', encoding='utf-8')
    assert snap() is None


def test_updates_counter_grows(pl):
    upd([{'step': 'A', 'status': 'in_progress'}])
    n1 = snap()['extra']['plan']['updates']
    upd([{'step': 'A', 'status': 'completed'}])
    assert snap()['extra']['plan']['updates'] > n1
