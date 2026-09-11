"""LLM-as-policy 奖励记忆库

记录 (state_key, strategy, reward)，按状态相似度检索高奖励历史决策，
作为 few-shot 示例注入 LLM prompt，实现上下文强化。

状态相似度基于 state_key 关键维度匹配（P0-4 起兼容 unified/legacy 多段 schema）：
- unified schema（6 段，rl_coordinator.UnifiedState.to_state_key 生成）：
  mode|emotion|affection|progress|curiosity|engage
- legacy MOBA schema（12 段，历史数据，仅兼容检索，不再新增）：
  hp|mp|enh|eth|uet|ally|enemy|lane|gold|lvl|jng|phase
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("reward_memory")

BASE_DIR = Path(__file__).parent.resolve()

# unified schema 维度（新格式，P0-4）
UNIFIED_KEY_FIELDS = (
    "mode", "emotion", "affection", "progress", "curiosity", "engage",
)
# legacy MOBA schema 维度（旧格式，兼容检索）
MOBA_KEY_FIELDS = (
    "hp", "mp", "enh", "eth", "uet", "ally", "enemy", "lane", "gold", "lvl", "jng", "phase",
)


class RewardMemory:
    def __init__(self, path: Optional[str] = None, max_entries: int = 3000, top_k: int = 4):
        self.path = Path(path) if path else BASE_DIR / "reward_memory.json"
        self.max_entries = max_entries
        self.top_k = top_k
        self.entries: list[dict] = []
        # (state_key, strategy) -> entries 下标索引，用于同状态同策略去重合并
        self._index: dict[tuple, int] = {}
        self._load()

    @staticmethod
    def _parse_key(state_key: str) -> Optional[dict]:
        """解析 state_key，兼容三种 schema（P0-4）。

        返回 dict 含 "schema" 字段（"unified" | "moba" | "legacy-short"）；无法解析返回 None。
        """
        parts = state_key.split("|")
        if len(parts) == len(UNIFIED_KEY_FIELDS):
            return {
                "schema": "unified",
                **{k: v for k, v in zip(UNIFIED_KEY_FIELDS, parts)},
            }
        if len(parts) >= len(MOBA_KEY_FIELDS):
            return {
                "schema": "moba",
                **{k: v for k, v in zip(MOBA_KEY_FIELDS, parts)},
            }
        if 1 < len(parts) < len(MOBA_KEY_FIELDS):
            # 其他历史段数（如 4 段 center_control/king_safety）——兼容保留，不丢数据
            return {
                "schema": "legacy-short",
                "parts": parts,
            }
        return None

    def _similarity(self, key_a: str, key_b: str) -> float:
        """状态相似度评分：关键维度匹配加权（P0-4）。

        - 同 schema：unified/moba 按维度权重；legacy-short 按前导段逐段计分
        - 跨 schema：按前导段匹配给低权重桥接分（避免 unified 查询断层）
        """
        a = self._parse_key(key_a)
        b = self._parse_key(key_b)
        if not a or not b:
            return 0.0
        if a["schema"] != b["schema"]:
            return self._cross_schema_sim(a, b)
        if a["schema"] == "legacy-short":
            pa, pb = a["parts"], b["parts"]
            n = min(len(pa), len(pb))
            if n == 0:
                return 0.0
            return sum(1.0 if pa[i] == pb[i] else 0.0
                       for i in range(n)) / n
        if a["schema"] == "moba":
            # 局势维度（权重高）：阶段、经济、兵线态势
            score = 0.0
            for dim in ("phase", "gold", "lane"):
                if a[dim] == b[dim]:
                    score += 2.0
            # 战术维度：附近敌我、状态、等级
            for dim in ("enemy", "ally", "enh", "eth", "uet", "lvl", "jng"):
                if a[dim] == b[dim]:
                    score += 1.0
            return score
        # unified schema：模式/情绪/互动权重高，亲密度/进度/好奇次之
        score = 0.0
        for dim in ("mode", "emotion", "engage"):
            if a[dim] == b[dim]:
                score += 2.0
        for dim in ("affection", "progress", "curiosity"):
            if a[dim] == b[dim]:
                score += 1.0
        return score

    @staticmethod
    def _cross_schema_sim(a: dict, b: dict) -> float:
        """跨 schema 桥接：前导段按序匹配，给低权重相似分（0..0.5），
        缓解 unified 查询在 legacy 数据上的检索断层。"""
        pa = a.get("parts") or [a.get(d) for d in (
            "mode", "emotion", "affection", "progress", "curiosity", "engage")
            if d in a]
        pb = b.get("parts") or [b.get(d) for d in (
            "mode", "emotion", "affection", "progress", "curiosity", "engage")
            if d in b]
        n = min(len(pa), len(pb))
        if n == 0:
            return 0.0
        hits = sum(1.0 for i in range(n) if pa[i] == pb[i])
        return hits / n * 0.5

    def store(self, state_key: str, strategy: str, reward: float,
              state_vec: Optional[list] = None) -> None:
        """记录一次决策的奖励（同状态同策略合并去重，保留奖励更显著的）

        state_vec 为可选的真实状态特征向量（RLHF 自动配对需要；历史数据可缺省）。
        """
        reward = float(reward)
        key = (state_key, strategy)
        idx = self._index.get(key)
        if idx is not None:
            # 已存在：保留奖励绝对值更大的一条（few-shot 示例更偏好显著奖励）
            if abs(reward) > abs(self.entries[idx]["reward"]):
                self.entries[idx]["reward"] = reward
                self.entries[idx]["ts"] = time.time()
            if state_vec is not None:
                self.entries[idx]["state_vec"] = [float(v) for v in state_vec]
            self._save()
            return
        entry: dict = {
            "state_key": state_key,
            "strategy": strategy,
            "reward": reward,
            "ts": time.time(),
        }
        if state_vec is not None:
            entry["state_vec"] = [float(v) for v in state_vec]
        self.entries.append(entry)
        self._index[key] = len(self.entries) - 1
        # 容量控制：保留最近 70%
        if len(self.entries) > self.max_entries:
            self._prune()
        self._save()

    def _prune(self) -> None:
        """容量裁剪：保留最近 70%，重建索引"""
        self.entries = self.entries[-int(self.max_entries * 0.7):]
        self._rebuild_index()

    def retrieve(self, state_key: str) -> list[dict]:
        """检索相似状态下高奖励的历史策略示例（去重策略，取 top_k）"""
        if not self.entries:
            return []
        scored = []
        for e in self.entries:
            sim = self._similarity(state_key, e["state_key"])
            if sim > 0:
                scored.append((sim, e["reward"], e))
        if not scored:
            # 无相似：返回全局最高奖励示例（冷启动期）
            scored = [(0.0, e["reward"], e) for e in self.entries]
        # 按 (相似度, 奖励) 排序
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        # 去重策略
        seen: set[str] = set()
        result = []
        for sim, rew, e in scored:
            strat = e["strategy"]
            if strat in seen:
                continue
            seen.add(strat)
            result.append({"strategy": strat, "reward": rew, "similarity": sim})
            if len(result) >= self.top_k:
                break
        return result

    def stats(self) -> dict:
        if not self.entries:
            return {"entries": 0, "avg_reward": 0.0}
        counts = {"unified": 0, "moba": 0, "legacy-short": 0}
        for e in self.entries:
            p = self._parse_key(e.get("state_key", ""))
            if p:
                counts[p["schema"]] = counts.get(p["schema"], 0) + 1
        return {
            "entries": len(self.entries),
            "strategies": len({e["strategy"] for e in self.entries}),
            "avg_reward": sum(e["reward"] for e in self.entries) / len(self.entries),
            "schema_counts": counts,
        }

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.entries, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"奖励记忆保存失败: {e}")

    def _rebuild_index(self) -> None:
        self._index = {}
        for i, e in enumerate(self.entries):
            self._index[(e["state_key"], e["strategy"])] = i

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # P0-4 数据清洗：剔除无法解析 state_key 的脏数据（格式污染遗留）
            cleaned = []
            schema_counts = {"unified": 0, "moba": 0, "legacy-short": 0, "invalid": 0}
            for e in raw:
                sk = e.get("state_key", "")
                parsed = self._parse_key(sk)
                if parsed is None:
                    schema_counts["invalid"] += 1
                    continue
                schema_counts[parsed["schema"]] += 1
                cleaned.append(e)
            if schema_counts["invalid"]:
                logger.info(f"奖励记忆清洗：剔除 {schema_counts['invalid']} 条无效 key")
            self.entries = cleaned
            # 一次性迁移去重：合并历史重复的 (state_key, strategy)，保留奖励更显著的一条
            merged: dict[tuple, dict] = {}
            for e in self.entries:
                key = (e.get("state_key", ""), e.get("strategy", ""))
                cur = merged.get(key)
                if cur is None:
                    merged[key] = e
                elif abs(e.get("reward", 0)) > abs(cur["reward"]):
                    merged[key] = {**e,
                                   "state_vec": e.get("state_vec") or cur.get("state_vec")}
                elif e.get("state_vec") is not None and cur.get("state_vec") is None:
                    # 去重合并时尽量保留其中一条携带的特征向量
                    merged[key] = {**cur, "state_vec": e["state_vec"]}
            self.entries = list(merged.values())
            self._rebuild_index()
            logger.info(
                f"加载奖励记忆 {len(self.entries)} 条"
                f"（unified {schema_counts['unified']} / legacy-moba {schema_counts['moba']} / legacy-short {schema_counts['legacy-short']}，"
                f"唯一策略 {len({e['strategy'] for e in self.entries})} 个）"
            )
        except Exception as e:
            logger.warning(f"奖励记忆加载失败: {e}")
            self.entries = []
            self._index = {}
