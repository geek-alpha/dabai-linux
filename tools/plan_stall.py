"""plan 清单搁置主动提醒：清单卡住没人管时，主动往对话里推一条。

任务中心里的「⏸ 已中断」只是**显示层**判定（tools/plan_view.py:90）——
没人盯着任务中心看的时候，搁置的清单就一直搁着。这里补上主动那一半：
后台定期扫一遍清单，超过阈值没推进、又有活没干完，就推一条提醒。

两条纪律：
1. 去重键是清单的 `updated_at`：同一版清单只提醒一次，推进了（updated_at 变）才可能再提；
2. 无人在线时**不标记**（送达数为 0 就别调 mark_reminded）——
   提醒没人看见等于没提，标记了才是真丢。

提醒范围比接力棒多一格：除了「有 in_progress 卡住」，也覆盖「有 pending 但一个
in_progress 都没有」——后者恰恰是最典型的搁置（清单挂在那，没有任何一步在跑）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_GLOBAL_PLAN = ROOT / "skills" / "tasks" / "data" / "agent_plan.json"
_USERS_ROOT = ROOT / "data" / "users"
DEFAULT_STALL_SEC = 600.0
STATE_FILE = ROOT / "data" / "plan_stall.json"
TASK_ID = "task-agent-plan"


def _threshold() -> float:
    """阈值可用 DABAI_PLAN_STALL_SEC 覆盖（测试要造搁置清单，不能真等十分钟）。"""
    try:
        return float(os.environ.get("DABAI_PLAN_STALL_SEC") or DEFAULT_STALL_SEC)
    except ValueError:
        return DEFAULT_STALL_SEC


def candidate_paths() -> list:
    """所有可能存在的清单文件：主人的全局一份 + 每个用户目录一份。"""
    out = []
    if _GLOBAL_PLAN.exists():
        out.append(str(_GLOBAL_PLAN))
    try:
        for d in sorted(_USERS_ROOT.glob("*/plan/agent_plan.json")):
            out.append(str(d))
    except Exception:
        pass
    return out


def _state_path(state_file=None) -> Path:
    return Path(state_file) if state_file else STATE_FILE


def _load_state(state_file=None) -> dict:
    p = _state_path(state_file)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("reminded"), dict):
                return data
    except Exception:
        pass
    return {"reminded": {}}


def _save_state(state: dict, state_file=None) -> None:
    p = _state_path(state_file)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        Path(tmp).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def _read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("plan"), list):
            return data
    except Exception:
        pass
    return {}


def format_text(item: dict) -> str:
    """提醒文案：几分钟没动、走到哪了、卡在哪一步。"""
    mins = max(1, int(item["idle_sec"] // 60))
    left = item["total"] - item["done"]
    head = (f"工作清单搁置 {mins} 分钟没动（{item['done']}/{item['total']}，"
            f"还有 {left} 步没收尾）")
    running = item.get("running") or []
    if running:
        return head + f"，进行中：{running[0]['step'][:40]}"
    return head + "，⚠️ 没有任何一步在进行中"


def pending_reminders(paths=None, now=None, threshold=None, state_file=None) -> list:
    """该提醒但还没提醒过的清单（纯查询，不改状态——标记由调用方在送达后做）。"""
    now = time.time() if now is None else float(now)
    thr = _threshold() if threshold is None else float(threshold)
    reminded = _load_state(state_file).get("reminded") or {}
    out = []
    for p in (paths if paths is not None else candidate_paths()):
        data = _read(p)
        plan = [s for s in (data.get("plan") or []) if isinstance(s, dict)]
        if not plan:
            continue
        done = sum(1 for s in plan if s.get("status") == "completed")
        total = len(plan)
        if done >= total:
            continue  # 全干完了不提醒：那是在等清空，不是搁置
        updated_at = float(data.get("updated_at") or 0)
        if not updated_at:
            continue
        idle = now - updated_at
        if idle <= thr:
            continue
        if reminded.get(str(p)) == updated_at:
            continue  # 同一版清单只提一次
        item = {
            "path": str(p),
            "updated_at": updated_at,
            "idle_sec": idle,
            "done": done,
            "total": total,
            "running": [s for s in plan if s.get("status") == "in_progress"],
            "task_id": TASK_ID,
        }
        item["text"] = format_text(item)
        out.append(item)
    return out


def mark_reminded(path, updated_at, state_file=None) -> None:
    """记下「这版清单已经提醒过了」。只在提醒真的送达（在线前端数 > 0）后调用。"""
    state = _load_state(state_file)
    reminded = state.setdefault("reminded", {})
    reminded[str(path)] = float(updated_at)
    _save_state(state, state_file)
