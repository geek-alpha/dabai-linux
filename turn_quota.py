"""普通用户每日轮次上限：沙箱管「能碰什么」，这里管「能烧多少」。

为什么要有它（2026-09-18）：
    sandbox.py 解决了越权，没解决花钱 —— 每个注册用户每聊一轮都走同一套 API key。
    没有上限，一个注册用户就是一台免费 LLM 代理：注册闸门只挡「进来」，
    进来之后烧多少没人管。

口径（只算「用户驱动的一轮」）：
    - 计入：真正开跑的用户对话轮（打字 / 语音转文字后），即 msg_source == 'chat'；
    - 不计：主动说话（proactive）、UI 自动上报（msg_source == 'auto'）、
      断点续跑（那一轮开跑时已经计过，续跑再计就是重复扣）；
    - 不限：管理员、未注册 uid（局域网统一身份 / CLI / 定时任务 = 系统身份）、
      显式豁免名单 —— 与 sandbox.actor_for 的判定口径一致；
    - 跨天自动清零（本地日期）。

配置 data/turn_quota.json：
    {"limit": 200, "exempt": ["u_xxxxxxxx"]}
    limit <= 0 表示不限。文件不存在按 DEFAULT_LIMIT；文件读坏同样按默认值 ——
    配置出错时宁可限得松，也不能因为读不到配置把所有人（含主人）锁死。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import auth_core

logger = logging.getLogger("turn_quota")

DATA_DIR = Path(__file__).resolve().parent / "data"
LIMIT_FILE = DATA_DIR / "turn_quota.json"
USAGE_FILE = DATA_DIR / "turn_quota_usage.json"
DEFAULT_LIMIT = 200

_lock = threading.Lock()


def _config() -> dict:
    try:
        cfg = json.loads(LIMIT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("turn_quota.json 读取失败，按默认值: %s", e)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def daily_limit() -> int:
    """每日轮次上限；<= 0 表示不限。"""
    try:
        return int(_config().get("limit", DEFAULT_LIMIT))
    except Exception:
        return DEFAULT_LIMIT


def _exempt_uids() -> set:
    raw = _config().get("exempt")
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_usage() -> dict:
    try:
        d = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    if d.get("date") != _today() or not isinstance(d.get("counts"), dict):
        d = {"date": _today(), "counts": {}}
    return d


def _save_usage(d: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_FILE.with_name(USAGE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(USAGE_FILE)


def exempt(uid: str) -> bool:
    """管理员 / 系统身份 / 豁免名单不受限。

    账号系统自身出问题时按系统身份放行：宁可少限一个人，也不能因为 users.json
    读不到就把主人锁在门外说不出话。
    """
    uid = str(uid or "").strip()
    if not uid:
        return True
    if uid in _exempt_uids():
        return True
    try:
        if auth_core.is_admin(uid):
            return True
        if not auth_core.get_user(uid):
            return True
    except Exception as e:
        logger.warning("额度豁免判定失败，按系统身份放行: %s", e)
        return True
    return False


def status(uid: str) -> dict:
    """额度快照（不消耗）。remaining=None 表示不限。"""
    limit = daily_limit()
    if exempt(uid) or limit <= 0:
        return {"limited": False, "limit": limit, "used": 0,
                "remaining": None, "date": _today()}
    with _lock:
        used = int(_load_usage()["counts"].get(str(uid), 0))
    return {"limited": True, "limit": limit, "used": used,
            "remaining": max(0, limit - used), "date": _today()}


def consume(uid: str) -> dict:
    """扣一轮额度。allowed=False 时这一轮不该开跑。"""
    uid = str(uid or "").strip()
    limit = daily_limit()
    if exempt(uid) or limit <= 0:
        return {"allowed": True, "limited": False, "limit": limit, "used": 0,
                "remaining": None, "date": _today()}
    with _lock:
        d = _load_usage()
        used = int(d["counts"].get(uid, 0))
        if used >= limit:
            return {"allowed": False, "limited": True, "limit": limit, "used": used,
                    "remaining": 0, "date": d["date"]}
        used += 1
        d["counts"][uid] = used
        try:
            _save_usage(d)
        except Exception as e:
            # 落盘失败不能让用户说不出话：进程内计数继续生效，只是重启后归零
            logger.warning("额度落盘失败（本轮仍放行）: %s", e)
        return {"allowed": True, "limited": True, "limit": limit, "used": used,
                "remaining": max(0, limit - used), "date": d["date"]}


def reset(uid: str = "") -> dict:
    """清零当天额度：uid 留空清全部，否则只清该用户（管理员手工放行用）。"""
    with _lock:
        d = _load_usage()
        if uid:
            d["counts"].pop(str(uid), None)
        else:
            d["counts"] = {}
        _save_usage(d)
        return d
