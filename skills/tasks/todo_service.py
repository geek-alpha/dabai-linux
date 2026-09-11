"""TODO 任务清单核心服务 —— 业务模型 + JSON 持久化。

本模块是插件的「业务层」，独立于插件入口与 HTTP API：
- 数据模型：任务（Task）与子任务（Subtask），支持优先级、截止时间、依赖、提醒、标签；
- 持久化：所有变更原子写入 skills/todo/data/tasks.json；
- 能力：任务的创建/更新/删除/查询、子任务维护、状态自动汇总、
  依赖关系与执行计划（拓扑排序）、提醒信息管理、统计。

其他模块（前端、其余插件、agent 工具）可以 `from todo_service
import TodoService` 直接复用，实现 REST API 之外的模块内 API。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger('todo_list.service')

# ---------- 状态与优先级常量（对外契约） ----------

# 任务状态机
STATUS_TODO = 'todo'                      # 待办
STATUS_IN_PROGRESS = 'in_progress'        # 进行中
STATUS_DONE = 'done'                      # 已完成
VALID_STATUS = (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE)

# 优先级（高/中/低）
PRIORITY_HIGH = 'high'
PRIORITY_MEDIUM = 'medium'
PRIORITY_LOW = 'low'
VALID_PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

# 提醒模式
REMIND_ONCE = 'once'                      # 一次性提醒
REMIND_REPEAT = 'repeat'                  # 重复提醒
VALID_REPEATS = ('none', 'minutely', 'hourly', 'daily', 'weekly')

# 提醒事件缓冲上限（防止无人消费时 tasks.json 无限增长）
MAX_REMINDER_EVENTS = 1000

# 优先级权重（用于排序）
_PRIORITY_WEIGHT = {PRIORITY_HIGH: 3, PRIORITY_MEDIUM: 2, PRIORITY_LOW: 1}

# 中英文别名映射（接受任意一种）
_PRIORITY_ALIASES = {
    'high': PRIORITY_HIGH, '高': PRIORITY_HIGH, '重要': PRIORITY_HIGH,
    '紧急': PRIORITY_HIGH, '紧急重要': PRIORITY_HIGH,
    'medium': PRIORITY_MEDIUM, '中': PRIORITY_MEDIUM, '普通': PRIORITY_MEDIUM,
    '一般': PRIORITY_MEDIUM,
    'low': PRIORITY_LOW, '低': PRIORITY_LOW, '不急': PRIORITY_LOW,
    '较低': PRIORITY_LOW,
}
_STATUS_ALIASES = {
    'todo': STATUS_TODO, '待办': STATUS_TODO, '未开始': STATUS_TODO,
    'pending': STATUS_TODO,
    'in_progress': STATUS_IN_PROGRESS, 'in progress': STATUS_IN_PROGRESS,
    'doing': STATUS_IN_PROGRESS, '进行中': STATUS_IN_PROGRESS,
    '已完成': STATUS_DONE, 'done': STATUS_DONE, '完成': STATUS_DONE,
}

DEFAULT_DATA_FILE = 'tasks.json'
DUE_TIME_FORMAT = '%Y-%m-%d %H:%M'
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


def normalize_priority(value) -> str:
    """把输入归一为合法的优先级枚举（high/medium/low），无法识别回退 medium。"""
    if value is None:
        return PRIORITY_MEDIUM
    key = str(value).strip().lower()
    return _PRIORITY_ALIASES.get(key, PRIORITY_MEDIUM)


def normalize_status(value) -> str:
    """把输入归一为合法的状态枚举（todo/in_progress/done），无法识别回退 todo。"""
    if value is None:
        return STATUS_TODO
    key = str(value).strip().lower()
    return _STATUS_ALIASES.get(key, STATUS_TODO)


def format_ts(ts, fmt=TIME_FORMAT) -> str:
    """时间戳 → 可读字符串。"""
    if not ts:
        return ''
    return time.strftime(fmt, time.localtime(float(ts)))


class TodoService:
    """TODO 服务：任务/子任务的增删改查、依赖执行计划、提醒状态、JSON 持久化。

    线程安全：内部使用 RLock，多线程（agent 工具、REST 请求、提醒线程）可并发访问。
    """

    def __init__(self, data_dir=None):
        self._lock = threading.RLock()
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent / 'data'
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._data_path = self.data_dir / DEFAULT_DATA_FILE
        self._tasks: dict = {}          # task_id -> 任务字典
        self._reminder_events: list = []  # 已触发但尚未被消费的提醒事件（供轮询/REST）
        self.load()

    # ==================== 持久化 ====================

    def load(self):
        """从 tasks.json 读取任务（文件缺失视为空库）。"""
        with self._lock:
            if self._data_path.exists():
                try:
                    with open(self._data_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._tasks = data.get('tasks', {}) if isinstance(data, dict) else {}
                    self._reminder_events = (data.get('reminder_events', [])
                                             if isinstance(data, dict) else [])
                except (json.JSONDecodeError, OSError, ValueError):
                    self._tasks = {}
                    self._reminder_events = []
            else:
                self._tasks = {}
                self._reminder_events = []

    def save(self):
        """原子写入 tasks.json（先写临时文件再替换，避免写坏数据）。"""
        with self._lock:
            payload = {'tasks': self._tasks, 'reminder_events': self._reminder_events}
            tmp = self._data_path.with_suffix('.tmp')
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._data_path)
            except OSError as e:
                logger.error('todo_list 持久化失败（数据仍在内存中）: %s -> %s', self._data_path, e)

    # ==================== 内部工具 ====================

    @staticmethod
    def _new_id(prefix='t') -> str:
        return f'{prefix}_{uuid.uuid4().hex[:12]}'

    def _get(self, task_id: str) -> dict:
        """取任务字典，不存在抛 KeyError（调用方负责捕获）。"""
        with self._lock:
            return self._tasks[task_id]

    def _snapshot(self) -> list:
        """返回全部任务的深拷贝列表（外部改动不会污染内部仓库）。"""
        with self._lock:
            return copy.deepcopy(list(self._tasks.values()))

    def _snapshot_one(self, task_id: str) -> dict | None:
        """返回单个任务的深拷贝，不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return copy.deepcopy(task) if task else None

    def _validate_dep_ids(self, task_id: str, ids, self_id=None) -> list:
        """校验依赖引用存在且非自引用，返回清洗后的 id 列表；非法则抛 ValueError。

        - 依赖可以是：其他任务 id（t_*）或同父任务下已存在的子任务 id（s_*）；
        - 自引用（依赖自身）与悬空引用（指向不存在的任务）均拒绝。
        """
        cleaned = self._clean_ids(ids)
        if not cleaned:
            return cleaned
        with self._lock:
            own_subs = {s['id'] for s in
                        (self._tasks.get(task_id, {}) or {}).get('subtasks', [])} if task_id else set()
            for dep in cleaned:
                if dep == self_id:
                    raise ValueError(f'任务不能依赖自身: {dep}')
                if dep in own_subs:
                    continue
                if dep not in self._tasks:
                    raise ValueError(f'依赖的任务不存在: {dep}')
        return cleaned

    # ==================== 任务 CRUD ====================

    def create_task(self, title: str, description: str = '',
                    priority=None, status=None, due_date=None,
                    depends_on=None, tags=None, notes: str = '') -> dict:
        """创建一个任务，返回任务字典。

        参数：
            title        任务标题（必填）
            description  详细描述
            priority     高/中/低（high/medium/low 或中文）
            status       待办/进行中/已完成
            due_date     截止时间（'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DD'，可带中文日期）
            depends_on   依赖的任务 id 列表
            tags         标签列表
            notes        备注
        """
        due_date, due_ts = self._parse_due(due_date)
        tid = self._new_id('t')
        task = {
            'id': tid,
            'title': str(title).strip(),
            'description': str(description or '').strip(),
            'priority': normalize_priority(priority),
            'status': normalize_status(status),
            'due_date': due_date or '',
            'due_ts': due_ts,
            'created_at': time.time(),
            'updated_at': time.time(),
            'depends_on': self._validate_dep_ids('', depends_on, self_id=tid),
            'subtasks': [],
            'reminder': None,
            'overdue_fired': False,
            'tags': [str(t).strip() for t in (tags or []) if str(t).strip()],
            'notes': str(notes or '').strip(),
        }
        with self._lock:
            self._tasks[task['id']] = task
            self.save()
        return copy.deepcopy(task)

    def _parse_due(self, due_date):
        """把用户给的截止时间解析为 (显示串, 时间戳)；解析失败返回 ('', None)。"""
        if not due_date:
            return '', None
        text = str(due_date).strip()
        from todo_parser import parse_datetime  # 延迟导入避免环依赖
        result = parse_datetime(text)
        if result is None:
            return text, None
        return result['text'], result['ts']

    def add_subtask(self, task_id: str, title: str, description: str = '',
                    priority=None, depends_on=None, due_date=None) -> dict:
        """给任务追加一个子任务，返回子任务字典（含可选的自身截止）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f'任务不存在: {task_id}')
            due, due_ts = self._parse_due(due_date)
            sub = {
                'id': self._new_id('s'),
                'title': str(title).strip(),
                'description': str(description or '').strip(),
                'priority': normalize_priority(priority) if priority else task['priority'],
                'status': STATUS_TODO,
                'due_date': due or '',
                'due_ts': due_ts,
                'depends_on': self._validate_dep_ids(task_id, depends_on),
            }
            task['subtasks'].append(sub)
            task['updated_at'] = time.time()
            self.save()
            return copy.deepcopy(sub)

    def update_task(self, task_id: str, **changes) -> dict:
        """更新任务字段（title/description/priority/status/due_date/tags/notes/depends_on）。

        返回更新后的任务字典。状态更新后自动重算父任务（见 _rollup_subtasks）。
        """
        allowed = {'title', 'description', 'priority', 'status',
                   'due_date', 'tags', 'notes', 'depends_on', 'overdue_fired'}
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f'任务不存在: {task_id}')
            touched = False
            for key, value in changes.items():
                if key not in allowed:
                    continue
                if key == 'priority':
                    value = normalize_priority(value)
                elif key == 'status':
                    value = normalize_status(value)
                elif key == 'due_date':
                    value, task['due_ts'] = self._parse_due(value)
                elif key == 'tags':
                    value = [str(t).strip() for t in (value or []) if str(t).strip()]
                elif key == 'depends_on':
                    value = self._validate_dep_ids(task_id, value, self_id=task_id)
                task[key] = value
                touched = True
            if not touched:
                return copy.deepcopy(task)
            task['updated_at'] = time.time()
            self.save()
            return task

    def delete_task(self, task_id: str) -> bool:
        """删除任务，同时清理其它任务对它的依赖引用。"""
        with self._lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
            for other in self._tasks.values():
                other['depends_on'] = [d for d in other.get('depends_on', []) if d != task_id]
            self.save()
            return True

    def purge_done(self, before_ts=None) -> int:
        """归档清理：删除已完成任务（可选只清理 updated_at 早于 before_ts 的）。

        不删除仍含未完成子任务的父任务；删除任务时同步清理其它任务的依赖引用。
        返回删除的任务数。
        """
        with self._lock:
            ids = []
            for tid, t in self._tasks.items():
                if t.get('status') != STATUS_DONE:
                    continue
                if before_ts is not None and float(t.get('updated_at') or 0) >= float(before_ts):
                    continue
                if any(s.get('status') != STATUS_DONE for s in t.get('subtasks', [])):
                    continue
                ids.append(tid)
            for tid in ids:
                del self._tasks[tid]
            for other in self._tasks.values():
                other['depends_on'] = [d for d in other.get('depends_on', []) if d not in ids]
            if ids:
                self.save()
            return len(ids)

    def delete_subtask(self, task_id: str, sub_id: str) -> bool:
        """删除子任务，同时清理其它子任务对它的依赖引用。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            subtasks = task.get('subtasks', [])
            target = next((s for s in subtasks if s['id'] == sub_id), None)
            if target is None:
                return False
            retained = [s for s in subtasks if s['id'] != sub_id]
            for s in retained:
                s['depends_on'] = [d for d in s.get('depends_on', []) if d != sub_id]
            task['subtasks'] = retained
            task['updated_at'] = time.time()
            self.save()
            return True

    # ==================== 子任务状态管理 ====================

    def update_subtask(self, task_id: str, sub_id: str, status=None,
                       priority=None, title=None, description=None) -> dict:
        """更新子任务字段；子任务状态变化后自动汇总父任务状态。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f'任务不存在: {task_id}')
            sub = next((s for s in task.get('subtasks', []) if s['id'] == sub_id), None)
            if sub is None:
                raise KeyError(f'子任务不存在: {sub_id}')
            if status is not None:
                sub['status'] = normalize_status(status)
            if priority is not None:
                sub['priority'] = normalize_priority(priority)
            if title is not None:
                sub['title'] = str(title).strip()
            if description is not None:
                sub['description'] = str(description).strip()
            task['updated_at'] = time.time()
            self._rollup_subtasks(task)
            self.save()
            return sub

    def _rollup_subtasks(self, task: dict):
        """父任务状态自动跟随子任务。

        规则：全部子任务已完成 → 父任务完成；
        任一子任务已有进展（进行中/已完成，即非待办）→ 父任务进行中；
        否则保持父任务原状态（防止覆盖用户主动设置）。
        """
        subs = task.get('subtasks', [])
        if not subs:
            return
        if all(s['status'] == STATUS_DONE for s in subs):
            if task['status'] != STATUS_DONE:
                task['status'] = STATUS_DONE
        elif any(s['status'] != STATUS_TODO for s in subs):
            if task['status'] == STATUS_TODO:
                task['status'] = STATUS_IN_PROGRESS

    # ==================== 查询 ====================

    def get_task(self, task_id: str) -> dict | None:
        """按 id 取任务（含子任务）的深拷贝，不存在返回 None。"""
        return self._snapshot_one(task_id)

    def get_tasks(self) -> list:
        """返回全部任务的深拷贝列表（按优先级降序、截止时间升序排列）。"""
        return self._sort_tasks(self._snapshot())

    @staticmethod
    def _sort_tasks(items) -> list:
        def weight(t):
            return _PRIORITY_WEIGHT.get(t.get('priority'), 2)
        return sorted(items, key=lambda t: (
            1 if t.get('status') == STATUS_DONE else 0,
            -weight(t),
            float(t.get('due_ts') or 0),
        ))

    def list_tasks(self, status=None, priority=None, due_before=None,
                   keyword=None) -> list:
        """按状态/优先级/截止时间前/关键词过滤任务。

        参数：
            status       'todo'|'in_progress'|'done'（或中文）
            priority     'high'|'medium'|'low'
            due_before   截止时间戳（仅返回截止时间在该时刻前的未完成任务）
            keyword      标题/描述/备注/标签包含关键词的任务
        """
        items = self._snapshot()
        want_status = normalize_status(status) if status else None
        want_pri = normalize_priority(priority) if priority else None
        kw = str(keyword or '').strip().lower()
        out = []
        for t in items:
            if want_status is not None and t['status'] != want_status:
                continue
            if want_pri is not None and t['priority'] != want_pri:
                continue
            if due_before is not None:
                # 与 docstring 一致：due_before 仅筛选未完成任务
                if t['status'] == STATUS_DONE:
                    continue
                if not (t.get('due_ts') and float(t['due_ts']) <= float(due_before)):
                    continue
            if kw:
                hay = ' '.join([t['title'], t['description'], t['notes'],
                                ' '.join(t['tags'])]).lower()
                if kw not in hay:
                    continue
            out.append(t)
        return self._sort_tasks(out)

    def find_subtask(self, task_id: str, sub_id: str) -> dict | None:
        """查指定任务下的子任务（深拷贝），不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            sub = next((s for s in task.get('subtasks', []) if s['id'] == sub_id), None)
            return copy.deepcopy(sub) if sub else None

    def stats(self) -> dict:
        """任务统计（用于工具/API 概览）。"""
        tasks = self._snapshot()
        counters = {'total': len(tasks)}
        for st in VALID_STATUS:
            counters[st] = sum(1 for t in tasks if t['status'] == st)
        counters['subtasks'] = sum(len(t.get('subtasks', [])) for t in tasks)
        counters['reminders'] = sum(1 for t in tasks if t.get('reminder'))
        counters['overdue'] = len(self.overdue_tasks())
        return counters

    def overdue_tasks(self) -> list:
        """返回已过期但未完成的任务（按优先级降序）。"""
        now = time.time()
        items = self._snapshot()
        out = [t for t in items
               if t.get('due_ts') and float(t['due_ts']) < now
               and t['status'] != STATUS_DONE]
        return self._sort_tasks(out)

    # ==================== 依赖与执行计划 ====================

    @staticmethod
    def _clean_ids(depends_on) -> list:
        if not depends_on:
            return []
        out = []
        for item in depends_on:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out

    def compute_execution_plan(self) -> dict:
        """根据任务/子任务依赖生成执行计划（拓扑排序）。

        返回：
            {
              'ok': bool,                  # False 表示存在依赖环
              'sequence': [...],           # 顺序执行的工作项
              'remaining': int,            # 未完成工作项数
            }
        工作项格式：{'kind':'task'|'subtask','id','title','priority','status','due_date',
        'dependency_met'}（dependency_met 表示其直接依赖在拓扑序中是否已全部完成）
        """
        tasks = self._snapshot()

        # 构造工作节点：父任务做容器排第一，然后是其未完成子任务；独立任务也可排序
        nodes = {}     # node_id -> work item（保持引用）
        edges = []     # (before_node, after_node)
        for t in tasks:
            tid = t['id']
            nodes[f'T:{tid}'] = {
                'kind': 'task', 'id': tid,
                'title': t['title'], 'priority': t['priority'],
                'status': t['status'], 'due_date': t.get('due_date') or '',
                'done': t['status'] == STATUS_DONE,
            }
            for d in t.get('depends_on') or []:
                edges.append((f'T:{d}', f'T:{tid}'))
            for s in t.get('subtasks') or []:
                sid = f'S:{tid}:{s["id"]}'
                nodes[sid] = {
                    'kind': 'subtask', 'id': s['id'], 'task_id': tid,
                    'title': s['title'], 'priority': s['priority'],
                    'status': s['status'], 'due_date': s.get('due_date') or '',
                    'done': s['status'] == STATUS_DONE,
                }
                edges.append((f'T:{tid}', sid))   # 子任务在父任务之后
                for d in s.get('depends_on') or []:
                    edges.append((f'S:{tid}:{d}', sid))

        ordered, cycle = self._toposort(nodes, edges)
        sequence = []
        for node_id in ordered:
            item = nodes[node_id]
            if item['done']:
                continue
            # dependency_met = 直接依赖的真实完成状态（status=done），
            # 而非"在拓扑序中排在前面"（悬空依赖视为未满足）
            deps = [b for (b, a) in edges if a == node_id and b in nodes]
            met = all(nodes[d]['done'] for d in deps)
            seq_item = dict(item)
            seq_item['dependency_met'] = met
            sequence.append(seq_item)

        remaining = sum(1 for n in nodes.values() if not n['done'])
        return {'ok': not cycle, 'cycle': cycle, 'sequence': sequence,
                'remaining': remaining}

    @staticmethod
    def _toposort(nodes: dict, edges: list) -> tuple:
        """Kahn 拓扑排序。返回 (有序 node_id 列表, 是否存在环路)。"""
        from collections import deque
        indegree = {nid: 0 for nid in nodes}
        adj = {nid: [] for nid in nodes}
        for before, after in edges:
            if before not in nodes or after not in nodes:
                continue
            adj[before].append(after)
            indegree[after] += 1
        queue = deque([nid for nid, d in indegree.items() if d == 0])
        ordered = []
        while queue:
            node = queue.popleft()
            ordered.append(node)
            for nxt in adj[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        cycle = len(ordered) != len(nodes)
        return ordered, cycle

    # ==================== 提醒管理 ====================

    def set_reminder(self, task_id: str, at=None, remind_type=None,
                     repeat='none', repeat_every=1) -> dict:
        """为任务设置（或修改）提醒。

        参数：
            at            提醒时刻（'YYYY-MM-DD HH:MM'、'HH:MM' 或中文表达，见 parser）
            remind_type   提醒类型：'once'（一次性）/ 'repeat'（重复）
            repeat        重复周期：'none'|'hourly'|'daily'|'weekly'（配合 remind_type=repeat）
            repeat_every  周期倍数（如每隔 2 天 → daily + 2）
        返回 {'ok', 'reminder'}；解析不了提醒时间则返回 {'ok': False, 'error': ...}
        """
        from todo_parser import parse_datetime, parse_hour_minute
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f'任务不存在: {task_id}')
            if at is None and repeat != 'none':
                # 无具体时刻的重复提醒（如"每2小时"）→ 从当前时刻起算
                now = time.time()
                result = {'ts': now,
                          'text': time.strftime(DUE_TIME_FORMAT, time.localtime(now)),
                          'repeat': repeat, 'repeat_every': repeat_every}
            else:
                result = parse_datetime(at) if at else None
            if at is not None and result is None:
                hm = parse_hour_minute(at)
                if not hm:
                    return {'ok': False, 'error': f'无法解析提醒时间: {at}'}
                now = time.time()
                base_ts = time.mktime(time.localtime(now))
                # 把 'HH:MM'/'晚上12点' 补全到今天（已过则推到明天；晚上12点顺延一天）
                base = time.localtime(base_ts)
                ts = time.mktime((base.tm_year, base.tm_mon, base.tm_mday,
                                  hm['hour'], hm['minute'], 0, 0, 0, -1))
                if hm.get('next_day'):
                    ts += 86400
                if ts < now:
                    ts += 86400
                result = {'ts': ts, 'text': time.strftime(DUE_TIME_FORMAT, time.localtime(ts)),
                          'repeat': repeat, 'repeat_every': repeat_every}
            if result is None:
                msg = '缺少提醒时间 at' if not at else f'无法解析提醒时间: {at}'
                return {'ok': False, 'error': msg}

            rtype = remind_type or (REMIND_REPEAT if repeat != 'none' else REMIND_ONCE)
            rtype = REMIND_REPEAT if repeat != 'none' else rtype
            reminder = {
                'type': rtype,
                'at': result.get('text', ''),
                'repeat': repeat if repeat in VALID_REPEATS else 'none',
                'repeat_every': max(1, int(repeat_every or 1)),
                'next_ts': result['ts'],
                'last_ts': None,
                'count': 0,
            }
            task['reminder'] = reminder
            task['updated_at'] = time.time()
            self.save()
            return {'ok': True, 'reminder': reminder}

    def clear_reminder(self, task_id: str) -> dict:
        """清除任务提醒。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f'任务不存在: {task_id}')
            reminder = task.get('reminder')
            task['reminder'] = None
            task['updated_at'] = time.time()
            self.save()
            return {'ok': True, 'cleared': bool(reminder)}

    def next_due_reminder_ts(self) -> float | None:
        """最近一条待触发的提醒时刻（供调度器休眠/对外展示）。"""
        with self._lock:
            ts_list = [float(t['reminder']['next_ts']) for t in self._tasks.values()
                       if t.get('reminder') and t['reminder'].get('next_ts')]
        return min(ts_list) if ts_list else None

    # ==================== 提醒触发（供调度器调用） ====================

    def _append_reminder_event(self, event: dict) -> None:
        """追加提醒事件到缓冲（有界：超出 MAX_REMINDER_EVENTS 丢弃最旧）。"""
        self._reminder_events.append(event)
        if len(self._reminder_events) > MAX_REMINDER_EVENTS:
            self._reminder_events = self._reminder_events[-MAX_REMINDER_EVENTS:]

    def fire_reminder(self, now=None, on_fire=None):
        """检查并触发已到期的提醒（定时 + 重复），到期任务顺带触发逾期提醒。

        on_fire 是可选的回调 on_fire(task, event)：
        event = {'kind': 'reminder'|'overdue', 'message': ..., 'ts': ...}
        每次触发都会追加进 _reminder_events 缓冲（REST 可轮询消费），再回调 on_fire。
        返回本轮触发的事件列表。
        """
        now = now or time.time()
        events = []
        with self._lock:
            for task in self._tasks.values():
                reminder = task.get('reminder')
                if reminder and reminder.get('next_ts') and float(reminder['next_ts']) <= now:
                    event = {
                        'kind': 'reminder',
                        'ts': now,
                        'task': task['id'],
                        'message': (f'⏰ 提醒：{task["title"]}'
                                    f'（优先级 {task["priority"]}'
                                    f'，截止 {task["due_date"] or "未设"}）'
                                    + (f'，子任务 {len(task["subtasks"])} 项'
                                       if task.get('subtasks') else '')),
                    }
                    events.append(event)
                    self._append_reminder_event(event)
                    reminder['count'] += 1
                    reminder['last_ts'] = now
                    timespec = reminder.get('repeat') or 'none'
                    if timespec == 'none':
                        reminder['next_ts'] = None           # 一次性：触发后不再自动触发
                    else:
                        reminder['next_ts'] = self._next_repeat_ts(
                            reminder['last_ts'], timespec, reminder.get('repeat_every', 1))
                if (task.get('due_ts') and not task.get('overdue_fired')
                        and float(task['due_ts']) < now
                        and task['status'] != STATUS_DONE):
                    event = {
                        'kind': 'overdue',
                        'ts': now,
                        'task': task['id'],
                        'message': (f'⏰ 任务已逾期：{task["title"]}'
                                    f'（截止 {task["due_date"]}）'),
                    }
                    events.append(event)
                    self._append_reminder_event(event)
                    task['overdue_fired'] = True
            if events:
                self.save()
        for event in events:
            task = self._tasks.get(event['task']) if event.get('task') else None
            if on_fire and task is not None:
                try:
                    on_fire(task, event)
                except Exception:
                    pass
        return events

    @staticmethod
    def _next_repeat_ts(last_ts, repeat='daily', every=1) -> float:
        import datetime as _dt
        base = _dt.datetime.fromtimestamp(last_ts)
        mult = max(1, int(every or 1))
        if repeat == 'minutely':
            base = base + _dt.timedelta(minutes=mult)
        elif repeat == 'hourly':
            base = base + _dt.timedelta(hours=mult)
        elif repeat == 'weekly':
            base = base + _dt.timedelta(weeks=mult)
        else:  # daily
            base = base + _dt.timedelta(days=mult)
        return base.timestamp()

    def pop_reminder_events(self, limit=None) -> list:
        """取出并清空已触发提醒事件缓冲（供 REST 轮询）。"""
        with self._lock:
            events = self._reminder_events
            if limit:
                events = events[: int(limit)]
            self._reminder_events = self._reminder_events[len(events):]
            self.save()
            return events

    def pending_reminder_events(self) -> list:
        """只读查看当前缓冲中的提醒事件（不消费）。"""
        with self._lock:
            return list(self._reminder_events)

    # ==================== 任务分解导入 ====================

    def import_plan(self, plan: dict, task_id=None) -> dict:
        """把 parser 分解出的执行计划落库。

        - 无 task_id：新建一个父任务，并追加子任务（带各自优先级/截止/依赖）；
        - 有 task_id：把计划子任务追加到既有任务下。
        返回 {'task': 父任务, 'subtasks': [...], 'execution_order': [...]}
        """
        title = str(plan.get('title') or '').strip() or '未命名任务'
        description = str(plan.get('goal') or '').strip()
        parent = self.create_task(
            title=title,
            description=description,
            priority=plan.get('priority'),
            due_date=plan.get('due_date'),
        ) if not task_id else self.get_task(task_id)
        if parent is None:
            raise KeyError(f'任务不存在: {task_id}')

        created = []
        order_map = {}
        for idx, item in enumerate(plan.get('subtasks') or []):
            sub = self.add_subtask(
                parent['id'],
                title=item.get('title') or f'子任务 {idx + 1}',
                description=item.get('description') or '',
                priority=item.get('priority'),
                due_date=item.get('due_date'),
            )
            created.append(sub)
            order_map[str(idx)] = sub['id']

        # 依赖按计划中的索引映射到新建子任务 id
        with self._lock:
            task = self._tasks.get(parent['id'])
            for idx, item in enumerate(plan.get('subtasks') or []):
                deps = [order_map[str(d)] for d in (item.get('depends_on') or [])
                        if str(d) in order_map]
                task['subtasks'][idx]['depends_on'] = deps
            task['updated_at'] = time.time()
            created = [dict(s) for s in task['subtasks']]   # 回读最新子任务（含依赖）
            self.save()

        order = []
        for idx in plan.get('execution_order') or []:
            if str(idx) in order_map:
                order.append(order_map[str(idx)])

        # 全局提醒落库：decompose 会把整段描述的提醒（如"每天9点提醒"）放进
        # plan['reminders']，这里落到父任务上（此前被完全丢弃）
        reminders = plan.get('reminders') or []
        if reminders and not self._tasks.get(parent['id'], {}).get('reminder'):
            r0 = reminders[0]
            try:
                self.set_reminder(parent['id'], at=r0.get('at'),
                                  remind_type=r0.get('type'),
                                  repeat=r0.get('repeat') or 'none',
                                  repeat_every=r0.get('repeat_every') or 1)
            except Exception as e:
                logger.warning('todo_list 导入全局提醒失败（忽略）: %s', e)

        return {'task': self.get_task(parent['id']), 'subtasks': created,
                'execution_order': order}

    # ==================== 序列化 ====================

    def to_json(self, tasks=None, pretty=True) -> str:
        """把任务列表序列化为 JSON 字符串（供 REST/模块间数据交互）。"""
        items = tasks if tasks is not None else self.get_tasks()
        return json.dumps({'tasks': items}, ensure_ascii=False, indent=2 if pretty else None)
