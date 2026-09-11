#!/usr/bin/env python3
"""P3-1 世界模型试点 —— Python 离线训练服务（DreamerV3 风格）

对接前端 Env 契约：经验样本为 JSON 四元组
    {"s": [stateVec...], "a": actionId, "r": reward, "s2": [nextStateVec...]}

功能：
1. 读取离线经验（前端导出 / 内置演示数据）
2. 训练世界模型（转移预测 + 奖励预测，MLP + 简化 LayerNorm）
3. 想象轨迹训练（imaginary rollout）：从真实状态出发，用世界模型
   预测未来状态与奖励，生成想象样本
4. 样本效率报告：对比"只用真实样本" vs "真实+想象样本"的策略训练曲线，
   验证想象增强能提升样本效率（验收指标）

纯标准库实现（无 numpy 依赖），冒烟测试可直接运行：
    python world_model_trainer.py --demo
    python world_model_trainer.py --train experiences.json --out wm_model.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path


# ==================== 轻量 MLP（纯 Python） ====================

def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class LayerNorm:
    """简化 LayerNorm：归一化 + 缩放平移（无仿射参数）"""

    def __init__(self, dim: int):
        self.dim = dim

    def forward(self, x):
        mean = sum(x) / len(x)
        var = sum((v - mean) ** 2 for v in x) / len(x)
        std = math.sqrt(var + 1e-5)
        return [(v - mean) / std for v in x], std


class WorldModelNet:
    """转移+奖励 MLP：输入 [s, onehot(a)] → 输出 [s', r]"""

    def __init__(self, state_size: int, n_actions: int, hidden: int = 32, seed: int = 2026):
        rnd = random.Random(seed)
        self.state_size = state_size
        self.n_actions = n_actions
        self.in_dim = state_size + n_actions
        self.out_dim = state_size + 1
        self.hidden = hidden
        # 两层权重 + 简化 LayerNorm
        self.w1 = [[rnd.uniform(-0.1, 0.1) for _ in range(self.in_dim)] for _ in range(hidden)]
        self.b1 = [0.0] * hidden
        self.w2 = [[rnd.uniform(-0.1, 0.1) for _ in range(hidden)] for _ in range(self.out_dim)]
        self.b2 = [0.0] * self.out_dim
        self.ln = LayerNorm(hidden)
        self.updates = 0

    def forward(self, inp):
        # 隐藏层：线性 → LN → SELU
        z1 = [sum(w[j] * inp[j] for j in range(self.in_dim)) + self.b1[i]
              for i, w in enumerate(self.w1)]
        ln_out, std = self.ln.forward(z1)
        a1 = [v if v > 0 else 1.0507 * (math.exp(v) - 1.0) if v <= 0 else 0.0 for v in ln_out]
        # 修正 SELU：alpha*(exp(x)-1)
        a1 = [v if v > 0 else 1.0507 * 1.6732 * math.expm1(v) for v in ln_out]
        # 输出层：线性
        out = [sum(self.w2[i][k] * a1[k] for k in range(self.hidden)) + self.b2[i]
               for i in range(self.out_dim)]
        return out, (a1, ln_out, std)

    def train_step(self, inp, target_s, target_r, lr: float):
        """单样本 SGD（带 SELU/LN 近似梯度）"""
        out, (a1, ln_in, std) = self.forward(inp)
        # 输出层误差
        d_out = [2.0 * (out[i] - (target_s[i] if i < self.state_size else target_r))
                 for i in range(self.out_dim)]
        # 输出层梯度
        g_w2 = [[d_out[i] * a1[k] for k in range(self.hidden)] for i in range(self.out_dim)]
        g_b2 = list(d_out)
        # 传播到隐藏层：d_a1 = sum_i d_out[i] * w2[i][k]
        d_a1 = [sum(d_out[i] * self.w2[i][k] for i in range(self.out_dim))
                for k in range(self.hidden)]
        # LN + SELU 梯度：d_ln = d_a1 * selu'(ln) / std
        for k in range(self.hidden):
            x = ln_in[k]
            selu_d = 1.0507 if x > 0 else 1.0507 * 1.6732 * math.exp(x)
            d_a1[k] = d_a1[k] * selu_d / std
        # 输入层梯度（含 LN 归一化近似，简化）
        n = self.hidden
        d_z1 = list(d_a1)
        g_w1 = [[d_z1[i] * inp[j] for j in range(self.in_dim)] for i in range(self.hidden)]
        g_b1 = list(d_z1)

        # 更新
        for i in range(self.out_dim):
            for k in range(self.hidden):
                self.w2[i][k] -= lr * g_w2[i][k]
            self.b2[i] -= lr * g_b2[i]
        for i in range(self.hidden):
            for j in range(self.in_dim):
                self.w1[i][j] -= lr * g_w1[i][j]
            self.b1[i] -= lr * g_b1[i]
        self.updates += 1
        return sum(d * d for d in d_out)


class WorldModelTrainer:
    """离线世界模型训练服务（DreamerV3 风格）"""

    MAX_EXPERIENCES = 300   # 库式调用（server.py 闭环）时内部经验样本上限

    def __init__(self, state_size: int, n_actions: int, seed: int = 2026):
        self.state_size = state_size
        self.n_actions = n_actions
        self.net = WorldModelNet(state_size, n_actions, seed=seed)
        self.experiences: list[dict] = []
        self.loss_history: list[float] = []

    # ---------------- 数据 ----------------

    def load_experiences(self, data) -> int:
        """加载经验样本（list[dict{s,a,r,s2}]）"""
        n = 0
        for e in data or []:
            if not e or "s" not in e or "s2" not in e or "a" not in e:
                continue
            self.experiences.append({
                "s": [float(v) for v in e["s"]],
                "a": int(e["a"]),
                "r": float(e.get("r", 0.0)),
                "s2": [float(v) for v in e["s2"]],
            })
            n += 1
        if len(self.experiences) > self.MAX_EXPERIENCES:
            self.experiences = self.experiences[-self.MAX_EXPERIENCES:]
        return n

    def _encode(self, s) -> list[float]:
        inp = list(s[:self.state_size])
        inp += [0.0] * self.n_actions
        return inp

    def _one_hot_apply(self, inp: list[float], a: int) -> list[float]:
        out = list(inp)
        out[self.state_size + a] = 1.0
        return out

    # ---------------- 训练 ----------------

    def train(self, epochs: int = 200, lr: float = 0.01, batch: int = 16) -> list[float]:
        if not self.experiences:
            return []
        losses = []
        for _ in range(epochs):
            sample = random.sample(self.experiences, min(batch, len(self.experiences)))
            total = 0.0
            for e in sample:
                inp = self._one_hot_apply(self._encode(e["s"]), e["a"])
                total += self.net.train_step(inp, e["s2"], e["r"], lr)
            losses.append(total / len(sample))
        self.loss_history.extend(losses)
        return losses

    # ---------------- 想象回放 ----------------

    def imagine_rollout(self, seed_states, policy_fn=None, horizon: int = 8) -> list[dict]:
        """从真实状态出发想象 rollout，返回想象样本 [(s,a,r,s2)]"""
        out = []
        for s0 in seed_states:
            s = list(s0)
            for _ in range(horizon):
                a = policy_fn(s) if policy_fn else random.randrange(self.n_actions)
                inp = self._one_hot_apply(self._encode(s), a)
                pred, _ = self.net.forward(inp)
                s2 = pred[:self.state_size]
                r = pred[self.state_size]
                out.append({"s": s, "a": a, "r": r, "s2": s2, "imagined": True})
                s = s2
        return out

    def report_sample_efficiency(self, steps: int = 40, seed_states=None) -> dict:
        """样本效率对比（验收指标）：
        仅真实样本训练 vs 真实+想象样本训练，比较同一训练步数下的预测误差。
        返回想象增强的相对提升百分比。
        """
        if len(self.experiences) < 8:
            return {"ok": False, "reason": "样本不足（>=8 才能对比）"}
        seeds = seed_states or [e["s"] for e in self.experiences[:4]]

        # A：仅真实样本
        net_a = WorldModelNet(self.state_size, self.n_actions, seed=99)
        loss_a = []
        for _ in range(steps):
            e = random.choice(self.experiences)
            inp = self._one_hot_apply(self._encode(e["s"]), e["a"])
            loss_a.append(net_a.train_step(inp, e["s2"], e["r"], 0.02))
        # 末端误差（最后 10 步均值）
        end_a = sum(loss_a[-10:]) / 10.0

        # B：真实 + 想象样本（每 5 步注入一次 rollout）
        net_b = WorldModelNet(self.state_size, self.n_actions, seed=99)
        loss_b = []
        for step in range(steps):
            e = random.choice(self.experiences)
            inp = self._one_hot_apply(self._encode(e["s"]), e["a"])
            loss_b.append(net_b.train_step(inp, e["s2"], e["r"], 0.02))
            if step % 5 == 4:
                imagined = self.imagine_rollout(seeds, horizon=3)
                for im in imagined:
                    inp2 = self._one_hot_apply(self._encode(im["s"]), im["a"])
                    loss_b.append(net_b.train_step(inp2, im["s2"], im["r"], 0.02))
        end_b = sum(loss_b[-10:]) / 10.0

        gain = (end_a - end_b) / (end_a + 1e-9)
        return {
            "ok": True,
            "realOnlyLoss": round(end_a, 4),
            "realPlusImaginedLoss": round(end_b, 4),
            "sampleEfficiencyGain": round(gain * 100, 2),   # % 提升
            "verdict": "样本效率提升" if gain > 0.01 else "无显著提升",
        }

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps({
            "state_size": self.state_size,
            "n_actions": self.n_actions,
            "w1": self.net.w1, "b1": self.net.b1,
            "w2": self.net.w2, "b2": self.net.b2,
            "updates": self.net.updates,
            "loss_history": self.loss_history[-200:],
        }, ensure_ascii=False), encoding="utf-8")

    def load(self, path: str) -> bool:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.state_size = data["state_size"]
            self.n_actions = data["n_actions"]
            self.net.w1 = data["w1"]; self.net.b1 = data["b1"]
            self.net.w2 = data["w2"]; self.net.b2 = data["b2"]
            self.net.updates = data.get("updates", 0)
            self.loss_history = data.get("loss_history", [])
            return True
        except Exception:
            return False


# ==================== 演示数据生成 ====================

def make_demo_experiences(state_size: int = 13, n_actions: int = 4, n: int = 120,
                          seed: int = 7) -> list[dict]:
    """生成带规律的可学习演示数据：next_state 是状态的确定性平移 + 动作依赖 + 噪声"""
    rnd = random.Random(seed)
    exps = []
    for _ in range(n):
        s = [rnd.random() for _ in range(state_size)]
        a = rnd.randrange(n_actions)
        # 可学习的转移规律：向动作方向平移 + 衰减
        s2 = [max(0.0, min(1.0, s[j] + 0.1 * math.sin(a * 0.8 + j) * (1 - s[j])))
              for j in range(state_size)]
        r = 0.5 * math.cos(a * 0.7) + 0.1 * sum(s) / state_size
        exps.append({"s": s, "a": a, "r": round(r, 4), "s2": s2})
    return exps


# ==================== CLI ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="P3-1 世界模型离线训练服务")
    parser.add_argument("--demo", action="store_true", help="运行演示（内置数据）")
    parser.add_argument("--train", metavar="JSON", help="训练经验文件路径")
    parser.add_argument("--out", default="world_model.json", help="模型输出路径")
    parser.add_argument("--epochs", type=int, default=150, help="训练轮数")
    parser.add_argument("--state-size", type=int, default=13, help="状态维度")
    parser.add_argument("--n-actions", type=int, default=4, help="动作数")
    args = parser.parse_args()

    if args.train:
        data = json.loads(Path(args.train).read_text(encoding="utf-8"))
    elif args.demo:
        data = make_demo_experiences(args.state_size, args.n_actions)
    else:
        parser.print_help()
        return 1

    trainer = WorldModelTrainer(args.state_size, args.n_actions)
    n = trainer.load_experiences(data)
    print(f"[P3-1] 已加载 {n} 条经验 (state={args.state_size}, actions={args.n_actions})")

    t0 = time.time()
    losses = trainer.train(epochs=args.epochs, lr=0.02)
    dt = time.time() - t0
    if losses:
        print(f"[P3-1] 训练完成: {len(losses)} 轮, {dt:.2f}s, "
              f"loss {losses[0]:.4f} -> {losses[-1]:.4f} (下降={losses[-1] < losses[0]})")

    report = trainer.report_sample_efficiency()
    print(f"[P3-1] 样本效率对比: {json.dumps(report, ensure_ascii=False)}")

    if args.out:
        trainer.save(args.out)
        print(f"[P3-1] 模型已保存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
