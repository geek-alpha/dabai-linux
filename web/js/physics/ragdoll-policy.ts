/* ============================================================
 * 策略控制器（浏览器侧）—— 把训练出来的网络接到物理上
 * ------------------------------------------------------------
 * 每帧三件事：按 policy-io 的口径拼观测 → 网络前向 → 写进物理残差。
 * 观测/动作的口径与训练环境共用 policy-io.ts，这里只负责时序
 * （相位、时间比例、上一帧动作的滚动）。
 *
 * phase 与 timeRatio 必须和训练时的分布对齐：训练里它们都随 episode
 * 时间线性推进、episode 长 600 帧（10 秒），所以这里也按 10 秒循环，
 * 否则策略会工作在它没见过的输入区间上。
 * ============================================================ */

import type { ActiveRagdoll } from './active-ragdoll.js';
import type { PolicyNet } from './policy-net.js';
import {
  OBS_DIM_FULL, ACT_DIM, ACTION_LIMIT,
  buildNetInput, writeAction,
} from './policy-io.js';

/** 与训练 episode 等长（600 帧 @60fps） */
const CYCLE_SECONDS = 10;

export class RagdollPolicy {
  readonly net: PolicyNet;
  private obs = new Float32Array(OBS_DIM_FULL);
  private prevAct = new Float32Array(ACT_DIM);
  private act = new Float32Array(ACT_DIM);
  private standHipY = 0.5;
  private t = 0;

  constructor(net: PolicyNet) {
    this.net = net;
  }

  /** 切换模型 / 重开物理时调用：清掉历史，重新量站立髋高 */
  reset(standHipY: number) {
    this.prevAct.fill(0);
    this.standHipY = Math.max(1e-3, standHipY);
    this.t = 0;
  }

  /** 每帧调用；内部完成观测→推理→写残差 */
  update(rd: ActiveRagdoll, dt: number) {
    this.t += dt;
    const cycle = this.t % CYCLE_SECONDS;
    const timeRatio = cycle / CYCLE_SECONDS;
    const phase = cycle * (2 * Math.PI / 2.4);
    buildNetInput(rd, this.obs, this.prevAct, this.standHipY, phase, timeRatio);
    this.net.forward(this.obs, this.act);
    writeAction(rd, this.act);
    for (let i = 0; i < ACT_DIM; i++) {
      const v = this.act[i];
      this.prevAct[i] = v > ACTION_LIMIT ? ACTION_LIMIT : v < -ACTION_LIMIT ? -ACTION_LIMIT : v;
    }
  }

  /** 关掉策略：清空残差，物理回到纯 PD */
  detach(rd: ActiveRagdoll) {
    rd.policyResidual = null;
  }

  get info(): Record<string, unknown> {
    return { obsDim: this.net.obsDim, actDim: this.net.actDim, meta: this.net.meta };
  }
}
