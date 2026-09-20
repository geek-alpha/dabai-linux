"""工作清单搁置提醒契约测试（tools/plan_stall.py）。

最容易做错的三条，钉死：
1. 同一版清单只提醒一次（去重键 = updated_at）——否则每 60s 一条，变刷屏；
2. 推进过（updated_at 变）必须能再提——否则提醒过一次就永久哑掉；
3. 全完成的清单不许提醒——那是在等清空，不是搁置。
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'tools'))

import plan_stall as S  # noqa: E402

T0 = 1_700_000_000.0


@pytest.fixture
def sf(tmp_path):
    """隔离的状态文件：去重记录不能污染真实 data/plan_stall.json。"""
    return tmp_path / 'plan_stall.json'


def mk(path, plan, updated_at, explanation=None):
    path.write_text(json.dumps({
        'plan': plan, 'updated_at': updated_at, 'explanation': explanation,
    }, ensure_ascii=False), encoding='utf-8')
    return str(path)


def steps(*pairs):
    return [{'step': s, 'status': st} for s, st in pairs]


def test_fresh_plan_not_reminded(tmp_path, sf):
    """刚推进过（idle < 阈值）不提醒。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('干活', 'in_progress')), T0)
    assert S.pending_reminders(paths=[p], now=T0 + 60, threshold=600, state_file=sf) == []


def test_stalled_with_in_progress_reminds(tmp_path, sf):
    """超阈值且有 in_progress → 提醒一条，文案含分钟数与卡住的那步。"""
    p = mk(tmp_path / 'agent_plan.json',
           steps(('读代码', 'completed'), ('改闸门', 'in_progress'), ('跑测试', 'pending')), T0)
    out = S.pending_reminders(paths=[p], now=T0 + 601, threshold=600, state_file=sf)
    assert len(out) == 1
    it = out[0]
    assert (it['done'], it['total'], it['task_id']) == (1, 3, S.TASK_ID)
    assert '10 分钟没动' in it['text']
    assert '1/3' in it['text'] and '改闸门' in it['text']


def test_no_in_progress_but_pending_reminds(tmp_path, sf):
    """没有任何一步在进行中，但还有 pending —— 最典型的搁置，也要提。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('甲', 'completed'), ('乙', 'pending')), T0)
    out = S.pending_reminders(paths=[p], now=T0 + 900, threshold=600, state_file=sf)
    assert len(out) == 1
    assert '没有任何一步在进行中' in out[0]['text']


def test_mark_reminded_dedupes(tmp_path, sf):
    """标记后同一版清单不再提醒（防每 60s 刷屏）。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('干活', 'in_progress')), T0)
    first = S.pending_reminders(paths=[p], now=T0 + 700, threshold=600, state_file=sf)
    assert len(first) == 1
    S.mark_reminded(p, first[0]['updated_at'], state_file=sf)
    assert S.pending_reminders(paths=[p], now=T0 + 900, threshold=600, state_file=sf) == []


def test_advanced_plan_can_remind_again(tmp_path, sf):
    """推进过（updated_at 变）就能再提一次——否则提醒过一次就永久哑掉。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('干活', 'in_progress')), T0)
    it = S.pending_reminders(paths=[p], now=T0 + 700, threshold=600, state_file=sf)[0]
    S.mark_reminded(p, it['updated_at'], state_file=sf)

    mk(tmp_path / 'agent_plan.json',
       steps(('干活', 'completed'), ('下一步', 'in_progress')), T0 + 800)
    again = S.pending_reminders(paths=[p], now=T0 + 800 + 700, threshold=600, state_file=sf)
    assert len(again) == 1
    assert '下一步' in again[0]['text']


def test_all_done_not_reminded(tmp_path, sf):
    """全完成不提醒：那是在等清空，不是搁置。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('甲', 'completed'), ('乙', 'completed')), T0)
    assert S.pending_reminders(paths=[p], now=T0 + 9999, threshold=600, state_file=sf) == []


def test_empty_or_missing_not_reminded(tmp_path, sf):
    """空清单 / 文件不存在都不提醒。"""
    empty = mk(tmp_path / 'a.json', [], T0)
    missing = str(tmp_path / 'nope.json')
    assert S.pending_reminders(paths=[empty, missing], now=T0 + 9999,
                               threshold=600, state_file=sf) == []


def test_broken_json_survives(tmp_path, sf):
    """配置损坏不能把后台扫描炸掉（扫描循环里抛异常 = 提醒永久停摆）。"""
    bad = tmp_path / 'bad.json'
    bad.write_text('{不是 json', encoding='utf-8')
    assert S.pending_reminders(paths=[str(bad)], now=T0 + 9999,
                               threshold=600, state_file=sf) == []


def test_missing_updated_at_not_reminded(tmp_path, sf):
    """没有 updated_at 的清单不提醒：算不出搁置时长，别瞎报。"""
    p = tmp_path / 'agent_plan.json'
    p.write_text(json.dumps({'plan': steps(('干活', 'in_progress'))}), encoding='utf-8')
    assert S.pending_reminders(paths=[str(p)], now=T0 + 9999,
                               threshold=600, state_file=sf) == []


def test_env_threshold_override(tmp_path, sf, monkeypatch):
    """阈值能被 DABAI_PLAN_STALL_SEC 覆盖（生产默认 10 分钟）。"""
    p = mk(tmp_path / 'agent_plan.json', steps(('干活', 'in_progress')), T0)
    monkeypatch.setenv('DABAI_PLAN_STALL_SEC', '5')
    assert len(S.pending_reminders(paths=[p], now=T0 + 6, state_file=sf)) == 1
    monkeypatch.setenv('DABAI_PLAN_STALL_SEC', 'abc')  # 脏值回退默认，不炸
    assert S.pending_reminders(paths=[p], now=T0 + 6, state_file=sf) == []


def test_state_roundtrip_and_broken_state(tmp_path, sf):
    """去重记录跨调用持久；状态文件损坏时当作空记录（宁重重一次，不永久哑掉）。"""
    S.mark_reminded('/x/agent_plan.json', 123.0, state_file=sf)
    assert S._load_state(sf)['reminded'] == {'/x/agent_plan.json': 123.0}
    sf.write_text('坏了', encoding='utf-8')
    assert S._load_state(sf)['reminded'] == {}


def test_multiple_plans_dedup_independently(tmp_path, sf):
    """多份清单（主人 + 各用户）各自去重，互不影响。"""
    a = mk(tmp_path / 'a.json', steps(('甲', 'in_progress')), T0)
    b = mk(tmp_path / 'b.json', steps(('乙', 'in_progress')), T0)
    out = S.pending_reminders(paths=[a, b], now=T0 + 700, threshold=600, state_file=sf)
    assert len(out) == 2
    S.mark_reminded(a, T0, state_file=sf)
    rest = S.pending_reminders(paths=[a, b], now=T0 + 800, threshold=600, state_file=sf)
    assert [r['path'] for r in rest] == [b]


def test_candidate_paths_covers_global_and_users(tmp_path, monkeypatch):
    """扫描范围：主人全局一份 + data/users/<uid>/plan 各一份。"""
    glob_plan = tmp_path / 'skills' / 'tasks' / 'data' / 'agent_plan.json'
    glob_plan.parent.mkdir(parents=True)
    glob_plan.write_text('{}', encoding='utf-8')
    users = tmp_path / 'data' / 'users'
    (users / 'u1' / 'plan').mkdir(parents=True)
    (users / 'u1' / 'plan' / 'agent_plan.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(S, '_GLOBAL_PLAN', glob_plan)
    monkeypatch.setattr(S, '_USERS_ROOT', users)
    got = S.candidate_paths()
    assert str(glob_plan) in got
    assert str(users / 'u1' / 'plan' / 'agent_plan.json') in got
