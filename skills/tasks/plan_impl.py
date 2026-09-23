"""Agent 自用工作清单（plan_*）—— 对标 codex 的 update_plan。

和 todo_* 的区别（这条最容易被混用）：
- todo_* 管的是**用户的任务**（用户提的需求、截止时间、提醒），有 task_id；
- plan_* 管的是**我自己当前这一轮/这件事的工作步骤**，是给用户看进度的清单。

状态机在工具层强制（codex 只写在提示词里，靠自觉）：
1. 同时最多 1 个 in_progress；
2. 从 pending 跳到 completed 要带 explanation —— 不是不许跳，是跳了要留痕；
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


def _check_transition(prev: list, new: list, explanation=None) -> tuple:
    """跳级检查：同一位置、同一文本的项从 pending 跳到 completed 时要求说明。

    返回 (err, meta)，meta 记录本次提交的变更性质，供 history 留痕与审计：
    jumps = 跳过的步骤序号（1-based），revised = 步骤被重排/增删/改文本。

    为什么不再硬拦跳级：检查按「同位置同文本」匹配，模型重排或改措辞即豁免
    （实测历史快照 7 步→10 步、完成 0→8 就是这么过去的）。硬拦拦不住想绕的人，
    只拦住了照抄文本的老实模型，代价是逼它多提交一次不含新信息的 in_progress
    快照。改成「跳级要带 explanation」：一轮里真做完的直接提交，不多跑往返；
    跨轮推进的仍被回显机制盯着逐步走。约束落在留痕上，不落在形式上。
    """
    jumps = []
    revised = len(prev) != len(new)
    for i, item in enumerate(new):
        if i >= len(prev):
            break
        p = prev[i]
        if not isinstance(p, dict):
            continue
        if p.get('step') != item['step']:
            revised = True
            continue
        if p.get('status') == 'pending' and item['status'] == 'completed':
            jumps.append(i + 1)
    meta = {'jumps': jumps, 'revised': revised}
    if jumps and not str(explanation or '').strip():
        names = '、'.join(f'第 {j} 项' for j in jumps[:3])
        more = f' 等 {len(jumps)} 步' if len(jumps) > 3 else ''
        return (f'{names}{more}从 pending 直接标 completed。'
                '同一轮里一次做完的，带 explanation 说一句（如「这 3 步一轮做完」）'
                '照常提交即可；跨轮推进的才需要先置 in_progress 再置 completed。', meta)
    return None, meta


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
        jump, meta = _check_transition(prev, steps, explanation)
        if jump:
            return jump
        if prev:
            entry = {
                'at': data.get('updated_at') or 0,
                'plan': prev,
                'explanation': data.get('explanation'),
            }
            # 变更性质随快照留痕：只看状态序列分不出「一轮做完」和「范围调整」——
            # 两者都表现为大跨度跳变，审计要能分开读，所以把 meta 落到条目上。
            if meta.get('jumps'):
                entry['jumps'] = meta['jumps']
                # 理由必须跟 jumps 同源：entry['explanation'] 存的是 prev 那一版的说明，
                # 跟本次提交无关，审计时读不出「为什么跳」，对不上号。
                entry['why'] = explanation
            if meta.get('revised'):
                entry['revised'] = True
            data.setdefault('history', []).append(entry)
            data['history'] = data['history'][-HISTORY_KEEP:]
        data['plan'] = steps
        data['updated_at'] = time.time()
        data['explanation'] = explanation
        _save(path, data)
    return _render(steps, explanation, title='工作清单已更新')


def read_plan() -> dict:
    """当前清单快照（供任务中心合成条目 / 外部只读消费）。

    created_at 取**首次提交时间**而不是 updated_at：任务中心按 created_at 倒序，
    用 updated_at 会让清单每步都跳到列表顶部（用户没提交也一直闪），
    用首次时间则位置稳定，只有它自己会随时间往下沉。
    """
    data = _load(_plan_path())
    plan = data.get('plan') or []
    hist = data.get('history') or []
    first_at = (hist[0].get('at') if hist else 0) or data.get('updated_at') or 0
    return {
        'plan': plan,
        'updated_at': data.get('updated_at') or 0,
        'created_at': first_at,
        'explanation': data.get('explanation'),
        'updates': len(hist) + (1 if plan else 0),
    }


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
        # 收尾闸门：有活没干完就想清，默认拒绝，必须显式 force。
        # 「清空」是唯一会让账本消失的动作：误清之后用户看到的进度凭空蒸发，也再
        # 分不清「做完了」和「没做就抹掉」。实测 8 份清单里 3 份提交一次就再没更新
        # 过，全靠 clear 抹平——闸门把「没做完就抹」变成一个有意识的动作。
        if left and not bool((args or {}).get('force')):
            lines = [f'工作清单还有 {len(left)} 步没完成，先别清空：']
            for s in left[:5]:
                lines.append(f'  {_MARK[s["status"]]} {s["step"][:40]}')
            if len(left) > 5:
                lines.append(f'  …还有 {len(left) - 5} 步')
            lines.append('两条出路：① 确实做完了 → plan_update 把它们标 completed'
                         '（同一轮一次做完的带 explanation 说明，跨轮的先 in_progress '
                         '再 completed，都整份提交）；'
                         '② 这件事放弃了 → 带 force=true 再调一次，history 会留痕。')
            return '\n'.join(lines)
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


def clear_finished() -> bool:
    """清单已全部完成时清空，返回是否清了。任务中心「清除已完成」用。

    合成条目不在 orchestrator / Harness 注册表里，批量清除够不着它 —— 所以这里
    补一个「按终态清数据源」的入口。没完成的清单一律不动：那是用户正在看的进度。
    走 _do_clear 同一把锁、同一份 history 归档，不另开写路径。
    """
    path = _plan_path()
    data = _load(path)
    plan = data.get('plan') or []
    if not plan or any(s.get('status') != 'completed' for s in plan):
        return False
    _do_clear(None)
    return True


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
        tag = ''
        if snap.get('jumps'):
            tag = f'  ⚡跳级 {len(snap["jumps"])} 步'
            why = str(snap.get('why') or '').strip()
            if why:
                tag += f' · {why[:30]}'
        elif snap.get('revised'):
            tag = '  ✎范围调整'
        lines.append(f'  {when}  {done}/{total} 完成{tag}')
        for s in snap.get('plan') or []:
            lines.append(f'      {_MARK.get(s.get("status"), "?")} {s.get("step", "")[:40]}')
    return '\n'.join(lines)


HANDLERS = {
    'plan_update': _do_update,
    'plan_show': _do_show,
    'plan_clear': _do_clear,
    'plan_history': _do_history,
}
