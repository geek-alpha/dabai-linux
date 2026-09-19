"""Plan Mode 闸门回归测试（对标 codex collaboration mode: plan）。

覆盖重点是**拒绝路径**：Plan Mode 下写类工具必须真被拦、只读工具必须真放行——
只测「进入/退出」两个状态等于没测，那正是「能力只写在提示词里」的老毛病。
"""
import json
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'skills', 'tasks'))

import plan_mode as PM  # noqa: E402
import plan_mode_impl as IMPL  # noqa: E402


@pytest.fixture
def pm(tmp_path, monkeypatch):
    f = tmp_path / 'plan_mode.json'
    monkeypatch.setattr(PM, '_path', lambda: str(f))
    monkeypatch.setattr(PM, '_uid', lambda: 'tester')
    return f


# ---- 未激活：一切放行（不能把正常模式也拦了）----

def test_inactive_allows_mutation(pm):
    assert PM.is_active() is False
    assert PM.check('code_edit') is None
    assert PM.check('shell_run') is None
    assert PM.prompt_block() == ''


# ---- 激活：写类工具必须被拒 ----

@pytest.mark.parametrize('tool', [
    'code_edit', 'code_create_file', 'code_append', 'code_patch', 'code_undo_turn',
    'shell_run', 'delegate_agent_task', 'sub_agent_spawn', 'harness_flow_submit',
    'todo_create', 'sched_add', 'image_gen_create', 'linux_gpio', 'linux_notify',
    'plan_update',  # codex 同样拒绝：Plan mode 下不用清单工具（plan.rs:87-91）
])
def test_active_blocks_mutation(pm, tool):
    PM.enter('t')
    deny = PM.check(tool)
    assert deny and tool in deny
    assert 'plan_mode' in deny  # 拒绝话术必须给出可执行出路


# ---- 激活：只读探索必须放行（否则 Plan Mode 第一步就卡死）----

@pytest.mark.parametrize('tool', [
    'code_read', 'code_search', 'code_locate', 'code_map', 'symbols', 'read_json',
    'code_git_diff', 'code_git_status', 'code_verify', 'code_test',
    'skill_help', 'system_check', 'linux_senses', 'find_file',
    'plan_show', 'plan_history', 'todo_list', 'harness_task_status',
])
def test_active_allows_read_only(pm, tool):
    PM.enter('t')
    assert PM.check(tool) is None


# ---- 状态机 ----

def test_enter_leave_cycle(pm):
    PM.enter('重构 plan 模块')
    assert PM.is_active() is True
    assert '重构 plan 模块' in PM.status_text()
    PM.leave()
    assert PM.is_active() is False
    assert PM.check('code_edit') is None
    assert PM.prompt_block() == ''


def test_switch_itself_never_blocked(pm):
    """进了出不来是致命的：开关工具必须永远可达。"""
    PM.enter('t')
    assert PM.check('plan_mode') is None


def test_leave_when_inactive(pm):
    assert '本来就不在' in PM.leave()


def test_state_persisted_to_disk(pm):
    PM.enter('t')
    data = json.loads(pm.read_text(encoding='utf-8'))
    assert data['tester']['active'] is True


def test_prompt_block_has_three_phases(pm):
    PM.enter('t')
    block = PM.prompt_block()
    for kw in ('阶段1', '阶段2', '阶段3', 'proposed_plan', 'decision complete'):
        assert kw in block


def test_tool_enter_exit_status(pm):
    r = IMPL._do_plan_mode({'action': 'enter', 'topic': 'x'})
    assert '已进入 Plan Mode' in r
    assert '进行中' in IMPL._do_plan_mode({'action': 'status'})
    assert '已退出' in IMPL._do_plan_mode({'action': 'exit'})
    assert '不在 Plan Mode' in IMPL._do_plan_mode({'action': 'status'})


def test_tool_default_action_is_status(pm):
    assert '不在 Plan Mode' in IMPL._do_plan_mode({})


def test_tool_unknown_action(pm):
    r = IMPL._do_plan_mode({'action': 'destroy'})
    assert '未知 action' in r
    assert 'enter' in r and 'exit' in r


# ---- 超时保护：忘了 exit 不能永远只读 ----

def _age(pm, seconds):
    """把状态文件里的进入时间往前拨，模拟停留了 seconds 秒。"""
    data = json.loads(pm.read_text(encoding='utf-8'))
    data['tester']['at'] = time.time() - seconds
    pm.write_text(json.dumps(data), encoding='utf-8')


def test_ttl_expiry_restores_mutation(pm):
    PM.enter('t', ttl_minutes=1)
    assert PM.is_active() is True
    _age(pm, 3600)
    assert PM.is_active() is False
    assert PM.check('code_edit') is None
    assert PM.check('shell_run') is None


def test_ttl_expiry_leaves_notice(pm):
    PM.enter('t', ttl_minutes=1)
    _age(pm, 3600)
    assert '超时' in PM.prompt_block()
    assert '超时' in PM.prompt_block()   # 第二次仍送达
    assert PM.prompt_block() == ''       # 第三次已自清，不刷屏


def test_ttl_zero_never_expires(pm):
    PM.enter('t', ttl_minutes=0)
    _age(pm, 86400)
    assert PM.is_active() is True
    assert PM.check('code_edit') is not None


def test_explicit_leave_leaves_no_notice(pm):
    """主动退出不该留下超时提示（那会让人以为是被踢出来的）。"""
    PM.enter('t')
    PM.leave()
    assert PM.prompt_block() == ''


def test_status_shows_remaining(pm):
    PM.enter('t', ttl_minutes=120)
    assert '距自动退出约 120 分钟' in PM.status_text()


def test_tool_enter_passes_ttl(pm):
    IMPL._do_plan_mode({'action': 'enter', 'topic': 'x', 'ttl_minutes': 30})
    assert '距自动退出约 30 分钟' in PM.status_text()


def test_tool_enter_bad_ttl_falls_back(pm):
    r = IMPL._do_plan_mode({'action': 'enter', 'topic': 'x', 'ttl_minutes': 'abc'})
    assert '已进入' in r
    assert '120 分钟' in r
