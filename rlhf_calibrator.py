#!/usr/bin/env python3
"""P3-3 RLHF-lite 奖励校准 —— Python 对比反馈通道

前端把"好/坏片段"对比对导出为 JSON：
    [
      {"good": {"s": [stateVec...], "a": 0, "r": [rewards...]},
       "bad":  {"s": [stateVec...], "a": 1, "r": [rewards...]}}
    ]

功能：
1. 读取对比反馈数据（前端导出 / 内置演示数据）
2. 训练 Bradley-Terry 偏好模型（片段聚合特征 → 偏好得分）
3. 输出奖励校准参数（偏好权重 w），供前端 shaping 使用
4. 验收指标：好片段平均得分 > 坏片段平均得分（分离度 sep > 0）

纯标准库实现（无 numpy 依赖）：
    python rlhf_calibrator.py --demo
    python rlhf_calibrator.py --train feedback.json --out rlhf_model.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def segment_feature(seg: dict, state_size: int, n_actions: int) -> list[float]:
    """片段聚合特征：均值状态 + 动作 one-hot 均值 + 奖励统计 + 长度"""
    s = seg.get("s", [])
    if not isinstance(s, list):
        s = []
    # 状态段必须是固定长度（state_size）；过短补 0、过长截断，
    # 否则后续 logit() 遍历 feat_dim 时会因特征不足触发越界。
    count = max(1, len(s))
    mean_s = [0.0] * state_size
    for i in range(min(state_size, len(s))):
        try:
            mean_s[i] = float(s[i]) / count
        except (TypeError, ValueError):
            mean_s[i] = 0.0

    mean_a = [0.0] * n_actions
    a_arr = seg.get("a", [])
    if not isinstance(a_arr, list):
        a_arr = [a_arr]
    if a_arr:
        for a in a_arr:
            if isinstance(a, (int, float)) and 0 <= int(a) < n_actions:
                mean_a[int(a)] += 1.0 / len(a_arr)

    r = seg.get("r", 0.0)
    r_list = r if isinstance(r, list) else [r]
    r_list = [float(v) for v in r_list]
    mean_r = sum(r_list) / max(1, len(r_list))
    var_r = sum((v - mean_r) ** 2 for v in r_list) / max(1, len(r_list))

    feat = mean_s + mean_a + [mean_r, var_r, min(1.0, len(r_list) / 60.0)]
    return feat


class RLHFLiteTrainer:
    """Bradley-Terry 偏好模型训练器"""

    MAX_PAIRS = 200   # 库式调用（server.py 闭环）时内部样本上限，防长跑内存膨胀

    def __init__(self, state_size: int, n_actions: int, seed: int = 2026):
        self.state_size = state_size
        self.n_actions = n_actions
        self.feat_dim = state_size + n_actions + 3
        self.w = [0.001] * self.feat_dim
        self.pairs: list[tuple[list[float], list[float]]] = []
        self.rng = random.Random(seed)
        self.loss_history: list[float] = []

    def load_pairs(self, data) -> int:
        """加载对比对 [{good:{s,a,r}, bad:{s,a,r}}]"""
        n = 0
        for p in data or []:
            if not p or "good" not in p or "bad" not in p:
                continue
            g = segment_feature(p["good"], self.state_size, self.n_actions)
            b = segment_feature(p["bad"], self.state_size, self.n_actions)
            if g and b:
                self.pairs.append((g, b))
                n += 1
        if len(self.pairs) > self.MAX_PAIRS:
            self.pairs = self.pairs[-self.MAX_PAIRS:]
        return n

    def logit(self, feat: list[float]) -> float:
        # 防御：权重/特征/声明维度三者任一偏短时只取共有部分，避免越界。
        n = min(self.feat_dim, len(self.w), len(feat))
        return sum(self.w[i] * feat[i] for i in range(n))

    def train(self, epochs: int = 200, lr: float = 0.2, l2: float = 0.01) -> list[float]:
        """Bradley-Terry 梯度上升：最大化 P(good > bad) = σ(g - b)"""
        losses = []
        for _ in range(epochs):
            if not self.pairs:
                break
            g, b = self.pairs[self.rng.randrange(len(self.pairs))]
            diff = self.logit(g) - self.logit(b)
            loss = -math.log(sigmoid(diff) + 1e-9)
            losses.append(loss)
            sig = sigmoid(diff)
            grad = (1.0 - sig)
            # 与 logit() 保持一致，仅更新实际共有的维度。
            n = min(self.feat_dim, len(g), len(b), len(self.w))
            for i in range(n):
                self.w[i] += lr * (grad * (g[i] - b[i]) - l2 * self.w[i])
        self.loss_history.extend(losses)
        return losses

    def measure_sep(self) -> float:
        """好/坏片段平均得分差（验收指标：> 0 表示模型区分出偏好）"""
        if not self.pairs:
            return 0.0
        g_sum = b_sum = 0.0
        for g, b in self.pairs:
            g_sum += self.logit(g)
            b_sum += self.logit(b)
        return (g_sum - b_sum) / len(self.pairs)

    def shaping(self, state_vec, action_id: int, reward_hint: float = 0.0) -> float:
        """奖励修正项（与前端 rlhf-lite.js 的 SHAPING_SCALE=0.5 一致）"""
        feat = segment_feature({"s": state_vec, "a": action_id, "r": reward_hint},
                               self.state_size, self.n_actions)
        raw = self.logit(feat)
        return max(-1.0, min(1.0, raw * 0.5))

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps({
            "state_size": self.state_size,
            "n_actions": self.n_actions,
            "w": self.w,
            "sep": self.measure_sep(),
            "pairs": len(self.pairs),
            "loss_history": self.loss_history[-200:],
        }, ensure_ascii=False), encoding="utf-8")

    def load(self, path: str) -> bool:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            file_state = int(data["state_size"])
            file_actions = int(data["n_actions"])
            # 模型维度必须与当前特征编码器一致：若直接用旧文件覆盖
            # state_size，会造成 feat_dim / 权重 / 特征三者失配（越界来源）。
            if (file_state, file_actions) != (self.state_size, self.n_actions):
                return False
            saved_w = list(data["w"])
            if len(saved_w) != self.feat_dim:
                return False
            self.w = saved_w
            self.loss_history = data.get("loss_history", [])
            return True
        except Exception:
            return False


# ==================== 演示数据 ====================

def make_demo_pairs(state_size: int = 13, n_actions: int = 4, n: int = 40,
                    seed: int = 3) -> list[dict]:
    """好片段：朝目标方向移动 + 正奖励；坏片段：远离目标 + 负奖励"""
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        a_good = rng.randrange(n_actions)
        a_bad = (a_good + 1) % n_actions
        s = [rng.random() for _ in range(state_size)]
        good = {
            "s": [s[j] for _f in range(6) for j in (range(state_size) if _f == 0 else [])] or s,
            "a": [a_good] * 6,
            "r": [2.0 + rng.random() for _ in range(6)],
        }
        bad = {
            "s": [s[j] for _f in range(6) for j in (range(state_size) if _f == 0 else [])] or s,
            "a": [a_bad] * 6,
            "r": [-1.0 - rng.random() for _ in range(6)],
        }
        pairs.append({"good": good, "bad": bad})
    return pairs


# ==================== CLI ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="P3-3 RLHF-lite 对比反馈校准")
    parser.add_argument("--demo", action="store_true", help="运行演示（内置数据）")
    parser.add_argument("--train", metavar="JSON", help="反馈数据文件路径")
    parser.add_argument("--out", default="rlhf_model.json", help="模型输出路径")
    parser.add_argument("--epochs", type=int, default=300, help="训练轮数")
    parser.add_argument("--state-size", type=int, default=13, help="状态维度")
    parser.add_argument("--n-actions", type=int, default=4, help="动作数")
    args = parser.parse_args()

    if args.train:
        data = json.loads(Path(args.train).read_text(encoding="utf-8"))
    elif args.demo:
        data = make_demo_pairs(args.state_size, args.n_actions)
    else:
        parser.print_help()
        return 1

    trainer = RLHFLiteTrainer(args.state_size, args.n_actions)
    n = trainer.load_pairs(data)
    print(f"[P3-3] 已加载 {n} 对对比反馈 (state={args.state_size}, actions={args.n_actions})")

    losses = trainer.train(epochs=args.epochs, lr=0.2)
    if losses:
        print(f"[P3-3] 训练完成: {len(losses)} 轮, loss {losses[0]:.4f} -> {losses[-1]:.4f}")

    sep = trainer.measure_sep()
    print(f"[P3-3] 好/坏分离度 sep={sep:.4f} "
          f"({'通过: 好片段得分 > 坏片段得分' if sep > 0.01 else '未通过: 模型未学到偏好'})")
    print(f"[P3-3] 示例 shaping(好动作)={trainer.shaping([0.5]*args.state_size, 0, 2.0):.3f}, "
          f"shaping(坏动作)={trainer.shaping([0.5]*args.state_size, 1, -1.0):.3f}")

    if args.out:
        trainer.save(args.out)
        print(f"[P3-3] 模型已保存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
