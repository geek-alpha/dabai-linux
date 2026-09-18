"""TODO 任务清单（todo）—— 使用说明见 SKILL.md。

实现层：业务逻辑在 todo_service.py（TodoService）、自然语言解析在 todo_parser.py
（decompose）、提醒调度在 todo_scheduler.py（ReminderScheduler）。
本文件只做三件事：
1. 把本目录加入 sys.path（供延迟导入上述模块）；
2. 提供 9 个 todo_* 工具的 HANDLERS 实现；
3. on_load/on_unload 启停提醒调度线程。
"""
from __future__ import annotations

import os
import sys
import threading

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

PROMPT = (
    '【TODO 任务清单插件已启用】当用户描述一项整体性/多步骤任务时，'
    '优先调用 todo_breakdown 把描述自动分解为关键任务与有序子任务并生成执行计划；'
    '用户提到优先级、截止时间、进度或提醒时，用 todo_create / todo_update /'
    ' todo_remind 管理任务；涉及依赖或先后步骤时调用 todo_plan 查看执行顺序。'
    '工具名都带 todo_ 前缀。子任务状态更新后父任务状态会自动汇总。'
)

# ---------- 按用户隔离的服务实例：服务 + 提醒调度器 ----------
# 每个执行者一份 TodoService，uid 从沙箱身份层取（user_store.current_uid），技能层
# 不传身份。空 uid = 主人/系统身份 → 技能目录里的全局文件（既有任务零迁移）；
# 普通用户 → data/users/<uid>/todo/tasks.json，互相看不见。

_services: dict = {}
_services_lock = threading.RLock()
_scheduler = None
_scheduler_lock = threading.Lock()


def _service_for(uid: str):
    with _services_lock:
        svc = _services.get(uid)
        if svc is None:
            import user_store
            from todo_service import TodoService
            data_dir = (user_store.scoped_dir('todo', uid) if uid
                        else os.path.join(_SKILL_DIR, 'data'))
            svc = TodoService(data_dir)
            _services[uid] = svc
        return svc


def _get_service():
    import user_store
    return _service_for(user_store.current_uid())


class _AllUserServices:
    """提醒调度器面对的门面：一轮检查遍历所有已实例化的用户库。

    调度器是常驻线程、只认一个 service，而库是按用户分的 —— 用门面把「检查所有
    用户」伪装成「检查一个 service」，调度器本身一行不用改。已建过的库留在
    _services 里不回收，用户设置完提醒就走开也照样会被检查到。
    """

    def fire_reminder(self, now=None, on_fire=None) -> list:
        with _services_lock:
            svcs = list(_services.values())
        out = []
        for svc in svcs:
            try:
                out.extend(svc.fire_reminder(now=now, on_fire=on_fire) or [])
            except Exception as e:  # noqa: BLE001 —— 单个用户的库坏了不能拖垮提醒线程
                print(f'[todo] 提醒检查失败（{svc.data_dir}）: {e}')
        return out


def _get_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            from todo_scheduler import ReminderScheduler
            _scheduler = ReminderScheduler(_AllUserServices())
        return _scheduler


def on_load(ctx):
    """技能加载时启动提醒调度线程（幂等）。"""
    try:
        _service_for('')  # 主人那份先建出来，提醒线程一起来就能检查到它
        _get_scheduler().start()
    except Exception as e:  # noqa: BLE001
        print(f'[todo] 提醒调度器启动失败: {e}')


def on_unload(ctx):
    """技能卸载时停止提醒调度线程。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.stop()
            except Exception as e:  # noqa: BLE001
                print(f'[todo] 提醒调度器停止失败: {e}')
            _scheduler = None


# ---------- 工具实现 ----------

def _do_breakdown(args) -> str:
    from todo_parser import decompose
    text = str(args.get('task_text') or args.get('text') or '').strip()
    if not text:
        return 'todo_breakdown 缺少 task_text（任务描述）'
    plan = decompose(text)
    if not plan.get('subtasks'):
        return '未能从描述中分解出可执行子任务，请换一种说法：\n' + plan['goal']
    created = ''
    if bool(args.get('create', True)):
        result = _get_service().import_plan(plan)
        task = result['task']
        created = f'已创建任务 {task["id"]}（标题：{task["title"]}）\n'
        lines = []
        for i, idx in enumerate(result['execution_order'], 1):
            sub = next(s for s in result['subtasks'] if s['id'] == idx)
            lines.append(f'  {i}. {sub["title"]}（优先级 {sub["priority"]}）')
        return (created + '执行计划（按依赖排序）：\n' + '\n'.join(lines)
                + '\n使用 todo_list / todo_get 查看，用 todo_update / todo_subtask 更新进度。')
    lines = []
    for i, idx in enumerate(plan.get('execution_order') or [], 1):
        sub = plan['subtasks'][idx]
        lines.append(f'  {i}. {sub["title"]}（优先级 {sub["priority"]}）')
    return ('解析结果（未落库，create=true 可创建）：\n' + '执行计划：\n'
            + '\n'.join(lines))


def _do_create(args) -> str:
    title = str(args.get('title') or '').strip()
    if not title:
        return 'todo_create 缺少 title'
    try:
        task = _get_service().create_task(
            title=title,
            description=str(args.get('description') or ''),
            priority=args.get('priority'),
            status=args.get('status'),
            due_date=args.get('due_date'),
            depends_on=args.get('depends_on'),
        )
        subtask_titles = args.get('subtasks') or []
        order = []
        prev = None
        for st in subtask_titles:
            sub = _get_service().add_subtask(task['id'], title=str(st).strip(),
                                             depends_on=[prev] if prev else None)
            order.append(sub['id'])
            prev = sub['id']
        extra = f'（{len(order)} 个子任务，顺序执行）' if order else ''
        return f'已创建任务 {task["id"]}：{task["title"]}，优先级 {task["priority"]}，' \
               f'状态 {task["status"]}' + (f'，截止 {task["due_date"]}' if task['due_date'] else '') \
               + extra
    except KeyError as e:
        return f'todo_create 失败：{e}'


def _do_plan(args) -> str:
    plan = _get_service().compute_execution_plan()
    if not plan['sequence']:
        return '当前没有待执行的工作项。'
    lines = []
    for i, item in enumerate(plan['sequence'], 1):
        lines.append(f'  {i}. [{item["priority"]}] {item["title"]}'
                     f'（{"子任务" if item["kind"] == "subtask" else "任务"}）')
    head = ('⚠️ 依赖存在环路，顺序仅供参考' if plan.get('cycle')
            else f'执行计划（{len(plan["sequence"])} 项依赖序）')
    return head + '：\n' + '\n'.join(lines)


def _do_list(args) -> str:
    items = _get_service().list_tasks(status=args.get('status'),
                                      priority=args.get('priority'),
                                      keyword=args.get('keyword'))
    if not items:
        return '当前没有符合条件的任务。'
    status_name = {'todo': '待办', 'in_progress': '进行中', 'done': '已完成'}
    pri_name = {'high': '高', 'medium': '中', 'low': '低'}
    lines = [f'任务清单（共 {len(items)} 项）：']
    for t in items:
        done = sum(1 for s in t['subtasks'] if s['status'] == 'done')
        sub_info = f'（子任务 {done}/{len(t["subtasks"])}）' if t['subtasks'] else ''
        lines.append(f'  [{status_name[t["status"]]}/{pri_name[t["priority"]]}] '
                     f'{t["id"]} {t["title"]}'
                     + (f' 截止 {t["due_date"]}' if t['due_date'] else '')
                     + sub_info)
    return '\n'.join(lines)


def _do_get(args) -> str:
    task = _get_service().get_task(str(args.get('task_id') or '').strip())
    if task is None:
        return '任务不存在'
    status_name = {'todo': '待办', 'in_progress': '进行中', 'done': '已完成'}
    pri_name = {'high': '高', 'medium': '中', 'low': '低'}
    lines = [f'任务 {task["id"]}：{task["title"]}',
             f'  状态：{status_name[task["status"]]}  优先级：{pri_name[task["priority"]]}',
             f'  截止：{task["due_date"] or "未设置"}',
             f'  描述：{task["description"] or "-"}']
    if task.get('reminder'):
        r = task['reminder']
        repeat_name = {'none': '一次性', 'hourly': '每小时',
                       'daily': '每天', 'weekly': '每周'}
        lines.append(f'  提醒：{r["at"]}（{repeat_name.get(r["repeat"], r["repeat"])}'
                     + (f' ×{r["repeat_every"]}' if r['repeat'] != 'none' else '')
                     + f'，已触发 {r["count"]} 次）')
    if task['subtasks']:
        lines.append('  子任务：')
        for s in task['subtasks']:
            mark = {'todo': '○', 'in_progress': '◐', 'done': '●'}[s['status']]
            lines.append(f'    {mark} {s["id"]} {s["title"]}'
                         f'（{pri_name[s["priority"]]}'
                         + (f'，截止 {s["due_date"]}' if s.get('due_date') else '') + '）')
    return '\n'.join(lines)


def _do_update(args) -> str:
    task_id = str(args.get('task_id') or '').strip()
    changes = {}
    for key in ('title', 'priority', 'status', 'due_date', 'description'):
        if args.get(key) is not None:
            changes[key] = args[key]
    if not changes:
        return 'todo_update 没有需要更新的字段'
    try:
        task = _get_service().update_task(task_id, **changes)
    except KeyError as e:
        return f'todo_update 失败：{e}'
    return (f'任务 {task_id} 已更新：{task["title"]}，'
            f'状态 {task["status"]}，优先级 {task["priority"]}'
            + (f'，截止 {task["due_date"]}' if task['due_date'] else ''))


def _do_subtask(args) -> str:
    task_id = str(args.get('task_id') or '').strip()
    sub_id = str(args.get('subtask_id') or '').strip()
    status = str(args.get('status') or args.get('subtask_status') or '').strip()
    try:
        sub = _get_service().update_subtask(task_id, sub_id, status=status)
    except KeyError as e:
        return f'todo_subtask 失败：{e}'
    task = _get_service().get_task(task_id)
    return (f'子任务 {sub_id}「{sub["title"]}」状态已改为 {sub["status"]}，'
            f'父任务状态自动汇总为 {task["status"]}')


def _do_remind(args) -> str:
    task_id = str(args.get('task_id') or '').strip()
    if bool(args.get('clear')):
        try:
            result = _get_service().clear_reminder(task_id)
        except KeyError as e:
            return f'todo_remind 失败：{e}'
        return f'任务 {task_id} 的提醒已清除'
    at = str(args.get('at') or '').strip()
    repeat = str(args.get('repeat') or 'none').strip()
    every = args.get('repeat_every') or 1
    if not at:
        return 'todo_remind 需要 at（提醒时刻），或用 clear=true 清除提醒'
    try:
        result = _get_service().set_reminder(task_id, at=at, remind_type=None,
                                             repeat=repeat, repeat_every=every)
    except KeyError as e:
        return f'todo_remind 失败：{e}'
    if not result.get('ok'):
        return f'todo_remind 失败：{result.get("error", "未知错误")}'
    r = result['reminder']
    repeat_name = {'none': '一次性', 'hourly': '每小时',
                   'daily': '每天', 'weekly': '每周'}
    return (f'任务 {task_id} 提醒已设置：{r["at"]}'
            + (f'，{repeat_name.get(r["repeat"], r["repeat"])}'
               + (f' ×{r["repeat_every"]}' if r['repeat'] != 'none' else '')))


def _do_delete(args) -> str:
    task_id = str(args.get('task_id') or '').strip()
    if not _get_service().delete_task(task_id):
        return f'todo_delete 失败：任务 {task_id} 不存在'
    return f'任务 {task_id} 已删除'


HANDLERS = {
    'todo_breakdown': _do_breakdown,
    'todo_create': _do_create,
    'todo_plan': _do_plan,
    'todo_list': _do_list,
    'todo_get': _do_get,
    'todo_update': _do_update,
    'todo_subtask': _do_subtask,
    'todo_remind': _do_remind,
    'todo_delete': _do_delete,
}
