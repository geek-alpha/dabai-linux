"""plan_* 工作清单状态机回归测试（对标 codex update_plan 的硬约束）。

覆盖重点是**拒绝路径**：双 in_progress、跳级不留痕、非法参数必须真的被工具层拦住
——只测放行路径等于没测。跳级这条是「不许跳」→「跳了要说一句」的新口径：
既测它该拒的时候拒，也测简单任务一轮做完能被放行。
"""
import json
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


def test_skip_without_explanation_rejected(pl):
    """跳级不再硬拦，但必须留痕：没带 explanation 就退回，并给出合法通道。

    旧口径是一律不许 pending→completed。它拦不住想绕的人（检查按同位置同文本
    匹配，重排或改措辞即豁免），只拦住了照抄文本的模型，代价是逼它多提交一次
    不含新信息的快照。现在改成「跳了要说一句」。
    """
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    r = upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    assert 'explanation' in r
    assert '一次做完' in r, '拒绝时必须告诉模型简单任务有合法通道，否则它只会当成硬拦'
    assert '○ A' in P._do_show({}), '拒绝了就不许动账本'


def test_skip_with_explanation_allowed_and_traced(pl):
    """一轮里一次做完：带 explanation 直接提交，零额外往返；快照记下跳了哪几步。"""
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    r = upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'completed'}],
            explanation='A、B 同一轮里一起做完')
    assert '已更新' in r
    data = json.loads(pl.read_text(encoding='utf-8'))
    assert data['history'][-1]['jumps'] == [1, 2]


def test_gradual_progress_leaves_no_jump_mark(pl):
    """逐步推进的快照不该被打上跳级/调整标记——否则审计读数全是噪音。"""
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'in_progress'}, {'step': 'B', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'in_progress'}])
    data = json.loads(pl.read_text(encoding='utf-8'))
    assert all('jumps' not in e for e in data['history'])
    assert all('revised' not in e for e in data['history'])


def test_reorder_marks_revised_not_jump(pl):
    """重排/增删是范围调整：放行，但要和「跳级」分开读——两者都表现为大跨度跳变。"""
    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    r = upd([{'step': 'B', 'status': 'pending'}, {'step': 'A', 'status': 'completed'}])
    assert '已更新' in r
    entry = json.loads(pl.read_text(encoding='utf-8'))['history'][-1]
    assert entry.get('revised') is True
    assert 'jumps' not in entry


def test_history_shows_jump_tag(pl):
    """留痕要能被读到：plan_history 得把跳级标出来，否则记了等于没记。"""
    upd([{'step': 'A', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'completed'}], explanation='一轮做完')
    assert '跳级' in P._do_history({'n': 3})


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


def test_clear_rejects_unfinished(pl):
    """收尾闸门：有活没干完就想清 → 拒绝，账本必须原样还在。

    旧行为是「警告一句然后照清」（`没完成就清了`）——实测 8 份清单里 3 份提交一次
    就再没更新过，全靠 clear 抹平，用户看到的进度凭空蒸发。现在默认拒绝。
    """
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    r = P._do_clear({})
    assert '先别清空' in r
    assert 'B' in r, '要点名是哪几步没完成，不然模型不知道从哪补'
    assert 'plan_update' in r, '拒绝时必须给出可执行的出路'
    assert '没完成' in P._do_show({}), '拒绝了就不许动账本'


def test_clear_force_clears_unfinished(pl):
    """确实放弃时带 force=true 才放行，旧清单进 history 留痕。"""
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}])
    r = P._do_clear({'force': True})
    assert '没完成就清了' in r
    assert '为空' in P._do_show({})


def test_clear_allows_finished_plan(pl):
    """全部完成时不该被闸门拦住——那正是闸门要放行的正常收尾。"""
    upd([{'step': 'A', 'status': 'in_progress'}])
    upd([{'step': 'A', 'status': 'completed'}])
    r = P._do_clear({})
    assert '全部步骤都已完成' in r


def test_clear_finished_helper_still_works(pl):
    """clear_finished() 走 _do_clear(None)，args 为 None 时不能把闸门打穿。"""
    upd([{'step': 'A', 'status': 'pending'}])
    assert P.clear_finished() is False, '有活没干完时不许清'
    upd([{'step': 'A', 'status': 'in_progress'}])
    upd([{'step': 'A', 'status': 'completed'}])
    assert P.clear_finished() is True


def test_clear_when_already_empty(pl):
    assert '本来就是空的' in P._do_clear({})


def test_path_falls_back_to_skill_dir(monkeypatch):
    """user_store 不可用（或匿名）时必须降级到技能目录，而不是抛异常。"""
    monkeypatch.setattr(P, '_plan_path', P.__dict__['_plan_path'])
    assert P._plan_path().endswith('agent_plan.json')


def test_history_records_jump_reason(pl):
    """跳级留痕必须带**同源**的说明。

    entry['explanation'] 存的是上一版清单的说明，跟本次提交无关；只看它读不出
    「这次为什么跳」。理由要跟 jumps 一起落到同一条快照上，审计才对得上号。
    """
    import json

    upd([{'step': 'A', 'status': 'pending'}, {'step': 'B', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'completed'}, {'step': 'B', 'status': 'pending'}],
        explanation='这步一轮就做完了')
    hist = json.loads(pl.read_text(encoding='utf-8'))['history']
    jumped = [h for h in hist if h.get('jumps')]
    assert jumped, '跳级没在 history 留痕'
    assert jumped[-1]['why'] == '这步一轮就做完了'

    shown = P._do_history({'n': 5})
    assert '⚡跳级' in shown
    assert '这步一轮就做完了' in shown, 'plan_history 没把跳级理由显示出来'


def test_history_reason_absent_when_no_jump(pl):
    """逐步推进不留 jumps/why，免得噪音盖住真跳级。"""
    import json

    upd([{'step': 'A', 'status': 'pending'}])
    upd([{'step': 'A', 'status': 'in_progress'}])
    upd([{'step': 'A', 'status': 'completed'}])
    hist = json.loads(pl.read_text(encoding='utf-8'))['history']
    assert all(not h.get('jumps') for h in hist)
    assert all('why' not in h for h in hist)
