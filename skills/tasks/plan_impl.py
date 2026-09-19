"""Agent 自用工作清单（plan_*）—— 对标 codex 的 update_plan。

和 todo_* 的区别（这条最容易被混用）：
- todo_* 管的是**用户的任务**（用户提的需求、截止时间、提醒），有 task_id；
- plan_* 管的是**我自己当前这一轮/这件事的工作步骤**，是给用户看进度的清单。

状态机在工具层强制（codex 只写在提示词里，靠自觉）：
1. 同时最多 1 个 in_progress；
2. 不许 pending 直接跳 completed —— 必须先经过 in_progress；
3. 每次提交整份清单（不是增量），历史快照留痕，可事后审计「有没有事后批量补完」。

整份提交是刻意的：codex 的 UpdatePlanArgs 也是 Vec<PlanItemArg> 全量替换，
它天然逼着模型每步重述全表，避免增量更新把清单改漂移。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

VALID_STATUS = ('pending', 'in_progress', 'completed')
_STATUS_CN = {'pending': '待办', 'in_progress': '进行中', 'completed': '已完成'}
_MARK = {'pending': '○', 'in_progress': '◐', 'completed': '●'}
MAX_STEPS = 20
HISTORY_KEEP = 20

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.RLock:
    with _locks_guard:
        lk = _locks.get(path)
        if lk is None:
            lk = _locks[path] = threading.RLock()
        return lk


def _plan_path() -> str:
    try:
        import user_store
        uid = user_store.current_uid()
        if uid:
            d = user_store.scoped_dir('plan', uid)
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, 'agent_plan.json')
    except Exception:
        pass
    d = os.path.join(_SKILL_DIR, 'data')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'agent_plan.json')


def _load(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('plan'), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {'plan': [], 'updated_at': 0, 'explanation': None, 'history': []}


def _save(path: str, data: dict) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _normalize(raw) -> tuple:
    """校验并归一化 plan 参数，返回 (steps, err)。"""
    if raw is None:
        return None, 'plan_update 缺少 plan 参数（[{step, status}, ...]）'
    if not isinstance(raw, list):
        return None, 'plan 必须是数组：[{"step": "...", "status": "pending|in_progress|completed"}]'
    if len(raw) > MAX_STEPS:
        return None, f'步骤最多 {MAX_STEPS} 条（当前 {len(raw)} 条）——别用填充步骤凑数'
    steps = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, f'第 {i} 项不是对象：{item!r}'
        step = str(item.get('step') or '').strip()
        if not step:
            return None, f'第 {i} 项缺 step 文本'
        if len(step) > 200:
            return None, f'第 {i} 项 step 太长（{len(step)} 字，上限 200）'
        status = str(item.get('status') or '').strip()
        if status not in VALID_STATUS:
            return None, (f'第 {i} 项 status={status!r} 非法，'
                          f'只能是 {" / ".join(VALID_STATUS)}')
        steps.append({'step': step, 'status': status})
    running = [s for s in steps if s['status'] == 'in_progress']
    if len(running) > 1:
        names = '、'.join(s['step'][:20] for s in running)
        return None, (f'同时只能有 1 个 in_progress，当前有 {len(running)} 个（{names}）。'
                      '先把前一个标 completed 再开下一个。')
    return steps, None


def _check_transition(prev: list, new: list) -> str | None:
    """跳级检查：同一位置、同一文本的项不许 pending → completed。"""
    for i, item in enumerate(new):
        if i >= len(prev):
            break
        p = prev[i]
        if not isinstance(p, dict):
            continue
        if p.get('step') != item['step']:
            continue
        if p.get('status') == 'pending' and item['status'] == 'completed':
            return (f'第 {i + 1} 项「{item["step"][:30]}」从 pending 直接跳到 completed。'
                    '必须先把状态置 in_progress，做完再置 completed——'
                    '先提交一次带 in_progress 的清单。')
    return None


def _render(plan: list, explanation=None, title='工作清单') -> str:
    if not plan:
        return f'{title}为空。'
    lines = [f'{title}（{len(plan)} 步）：']
    for i, s in enumerate(plan, 1):
        lines.append(f'  {i}. {_MARK[s["status"]]} {s["step"]}'
                     f'（{_STATUS_CN[s["status"]]}）')
    done = sum(1 for s in plan if s['status'] == 'completed')
    running = [s for s in plan if s['status'] == 'in_progress']
    tail = f'进度 {done}/{len(plan)}'
    if running:
        tail += f'，进行中：{running[0]["step"][:30]}'
    elif done < len(plan):
        tail += '，⚠️ 没有 in_progress 项——下一步该做什么就把它置为 in_progress'
    lines.append(tail)
    if explanation:
        lines.append(f'调整说明：{explanation}')
    return '\n'.join(lines)


def _do_update(args) -> str:
    steps, err = _normalize(args.get('plan'))
    if err:
        return err
    explanation = args.get('explanation')
    explanation = str(explanation).strip() if explanation else None
    path = _plan_path()
    with _lock_for(path):
        data = _load(path)
        prev = data.get('plan') or []
        jump = _check_transition(prev, steps)
        if jump:
            return jump
        if prev:
            data.setdefault('history', []).append({
                'at': data.get('updated_at') or 0,
                'plan': prev,
                'explanation': data.get('explanation'),
            })
            data['history'] = data['history'][-HISTORY_KEEP:]
        data['plan'] = steps
        data['updated_at'] = time.time()
        data['explanation'] = explanation
        _save(path, data)
    return _render(steps, explanation, title='工作清单已更新')


def _do_show(args) -> str:
    path = _plan_path()
    data = _load(path)
    plan = data.get('plan') or []
    out = _render(plan, data.get('explanation'))
    if plan:
        import datetime
        ts = data.get('updated_at') or 0
        if ts:
            out += f'\n（最后更新 {datetime.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}）'
        left = [s for s in plan if s['status'] != 'completed']
        if left:
            out += f'\n还有 {len(left)} 步没完成，别急着收尾。'
    return out


def _do_clear(args) -> str:
    path = _plan_path()
    with _lock_for(path):
        data = _load(path)
        if not (data.get('plan') or []):
            return '工作清单本来就是空的。'
        left = [s for s in data['plan'] if s['status'] != 'completed']
        data.setdefault('history', []).append({
            'at': data.get('updated_at') or 0,
            'plan': data['plan'],
            'explanation': data.get('explanation'),
        })
        data['history'] = data['history'][-HISTORY_KEEP:]
        data['plan'] = []
        data['updated_at'] = time.time()
        data['explanation'] = None
        _save(path, data)
    if left:
        return f'工作清单已清空（有 {len(left)} 步没完成就清了：{left[0]["step"][:30]} …）'
    return '工作清单已清空（全部步骤都已完成）。'


def _do_history(args) -> str:
    path = _plan_path()
    data = _load(path)
    hist = data.get('history') or []
    if not hist:
        return '还没有历史快照。'
    import datetime
    n = int(args.get('n') or 5)
    lines = [f'最近 {min(n, len(hist))} 次快照（共 {len(hist)} 次）：']
    for snap in hist[-n:]:
        ts = snap.get('at') or 0
        when = f'{datetime.datetime.fromtimestamp(ts):%m-%d %H:%M:%S}' if ts else '?'
        done = sum(1 for s in snap.get('plan') or [] if s.get('status') == 'completed')
        total = len(snap.get('plan') or [])
        lines.append(f'  {when}  {done}/{total} 完成')
        for s in snap.get('plan') or []:
            lines.append(f'      {_MARK.get(s.get("status"), "?")} {s.get("step", "")[:40]}')
    return '\n'.join(lines)


HANDLERS = {
    'plan_update': _do_update,
    'plan_show': _do_show,
    'plan_clear': _do_clear,
    'plan_history': _do_history,
}
