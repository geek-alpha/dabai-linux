# -*- coding: utf-8 -*-
"""按用户隔离的本地数据文件路径解析。

规则（按序判定）：
  1) 显式传入 uid → data/users/<uid>/<name>；
  2) 没传 uid → 从沙箱身份层取当前执行者（sandbox.current() 的 Actor）；
  3) 取不到，或执行者是管理员/系统身份 → 项目根目录的全局文件。

为什么第 2 条要读身份层（2026-09-16）：
    技能层不该知道调用者是谁。视频收藏、歌单这些存储散落在 skills/ 里，如果
    靠每个调用点自己传 uid，就得在技能层铺一遍身份传递 —— 漏一处就是一个越权
    读写。身份的唯一来源是 sandbox 的 Actor contextvar（agent 执行工具前 push），
    在这里读它，所有按用户隔离的存储自动生效，技能层一行不用改。

第 3 条保留全局文件，是因为「本机主人」与「没有用户上下文」（CLI、定时任务、
局域网直连、AI 工具链在无 actor 时）都属于部署者本人，与既有行为一致，
主人的历史数据零迁移。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USER_DATA_ROOT = ROOT / 'data' / 'users'

_UID_RE = re.compile(r'[^A-Za-z0-9_-]')
# 畸形 uid（清洗后为空）的隔离目录：宁可给它一个空目录，也不能回落到全局文件
# —— 那是主人的数据，回落等于把越权读写成默认行为。
_INVALID_UID = '_invalid'


def safe_uid(uid) -> str:
    """把 uid 清洗成可安全当目录名的一段；畸形 uid 返回空串。"""
    return _UID_RE.sub('', str(uid or ''))[:64]


def current_uid() -> str:
    """当前执行者 uid（来自沙箱身份层）；管理员与系统身份返回空串。

    空串的含义是「落在全局文件」，见模块文档第 3 条。
    """
    try:
        import sandbox

        actor = sandbox.current()
    except Exception:
        return ''
    if actor is None or actor.is_admin:
        return ''
    return str(actor.uid or '')


def user_file(name: str, uid=None) -> Path:
    """某个用户某份数据的文件路径；目录不存在则创建。

    显式 uid 优先；没传就按当前执行者身份落文件 —— 技能层不必传 uid。
    """
    uid = uid or current_uid()
    if not uid:
        return ROOT / name
    d = USER_DATA_ROOT / (safe_uid(uid) or _INVALID_UID)
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def scoped_dir(name: str = "", uid=None, global_dir=None) -> Path:
    """按用户隔离的**目录**（一份数据拆成多个文件、或文件名由调用方决定时用）。

    普通用户 → data/users/<uid>/<name>；主人/系统身份 → global_dir（缺省项目根）。
    目录不存在则创建。
    """
    uid = uid or current_uid()
    if not uid:
        d = Path(global_dir) if global_dir else ROOT
    else:
        d = USER_DATA_ROOT / (safe_uid(uid) or _INVALID_UID)
        if name:
            d = d / name
    d.mkdir(parents=True, exist_ok=True)
    return d
