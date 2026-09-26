/* ============================================================
 * 布娃娃强化学习环境 —— 与浏览器共用同一份物理代码
 * ------------------------------------------------------------
 * 环境不重写物理：直接 new 浏览器里那个 ActiveRagdoll，只是把
 * 「动画系统每帧写骨骼」这一步换成手工 applyPose。所以训练出来的
 * 策略面对的就是前端真实的接触动力学，不是另一套近似模型。
 *
 * 复杂物理环境由四层随机化拼出来：
 *   1. 本体      scale / PD 增益 / 髋部支撑 balance / 重力
 *   2. 地形      Minecraft 式体素方块高度场（平地/楼梯坡/噪声地形+台阶+坑），摩擦随机
 *   3. 初始条件  关节姿态扰动、初速度扰动
 *   4. 持续扰动  随机时刻、随机方向的水平冲量（推、撞、风）
 * balance 随机到 0.15 意味着「动画几乎不托身体」—— 策略必须在
 * 这种条件下也站得住，前端才敢把手工辅助关掉。
 * ============================================================ */

import * as THREE from 'three';
import { buildRig, snapshotPose, applyPose, STAND_HIP_Y, BASE_HEIGHT } from './skeleton.mjs';
import { VoxelTerrain, KIND_FLAT, KIND_SLOPE, KIND_VOXEL, KIND_NAMES } from './terrain.mjs';
import {
  ActiveRagdoll, loadRapier, SEGMENTS,
  OBS_DIM, OBS_DIM_FULL, ACT_DIM, PART_COUNT,
  buildObservation, writeAction,
} from './build/ragdoll-core.mjs';

export { KIND_FLAT, KIND_SLOPE, KIND_VOXEL, KIND_NAMES };

export const EPISODE_STEPS = 600;
const DT = 1 / 60;

/* 程序化 idle 目标姿态：真实动画里身体一直在动（重心转移、手臂摆、膝
 * 屈伸），PD 硬跟这种目标会在扰动下振荡失稳 —— 这正是策略要学的。
 * amp 单位弧度，freq 是相对基准频率的倍数。
 * 腿部单独一组（leg:true）：前端一播走路动画，腿的前后摆就是 25~35°，
 * 而这里原本只有 2~3° —— 策略从没见过大摆幅目标，物理的 PD 拉力和残差
 * 在同一条腿上对拉，腿被推成侧向张开（实测腿侧向 0.18 → 0.51）。 */
const SWAY = [
  { bone: 'hips', axis: [0, 0, 1], amp: 0.045, freq: 1 },
  { bone: 'hips', axis: [1, 0, 0], amp: 0.02, freq: 2 },
  { bone: 'spine', axis: [0, 0, 1], amp: -0.025, freq: 1 },
  { bone: 'chest', axis: [0, 1, 0], amp: 0.05, freq: 0.5 },
  { bone: 'head', axis: [0, 1, 0], amp: 0.08, freq: 0.5 },
  { bone: 'leftUpperArm', axis: [0, 0, 1], amp: 0.07, freq: 1 },
  { bone: 'rightUpperArm', axis: [0, 0, 1], amp: -0.07, freq: 1 },
  { bone: 'leftUpperLeg', axis: [1, 0, 0], amp: 0.32, freq: 2, leg: true },
  { bone: 'rightUpperLeg', axis: [1, 0, 0], amp: -0.32, freq: 2, leg: true },
  { bone: 'leftLowerLeg', axis: [1, 0, 0], amp: -0.30, freq: 2, leg: true },
  { bone: 'rightLowerLeg', axis: [1, 0, 0], amp: 0.30, freq: 2, leg: true },
];
const _swayQ = new THREE.Quaternion();
const _swayAxis = new THREE.Vector3();
const _qA = new THREE.Quaternion();
const _qB = new THREE.Quaternion();
const _qC = new THREE.Quaternion();

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class RagdollEnv {
  constructor({ randomize = true, episodeSteps = EPISODE_STEPS } = {}) {
    this.randomize = randomize;
    this.episodeSteps = episodeSteps;
    this.dt = DT;
    this.RAPIER = null;
    this.rd = null;
    /** 网络输入缓冲（obs + 上一帧动作） */
    this.obs = new Float32Array(OBS_DIM_FULL);
    this.prevAct = new Float32Array(ACT_DIM);
    this.action = new Float32Array(ACT_DIM);
    this.t = 0;
    this.terrain = KIND_FLAT;
    /** 地形生成器实例（每局重建） */
    this.voxel = null;
    /** 强制地形类型（评估分项用），null = 随机 */
    this.forceTerrain = null;
    /** 强制开局姿态（评估分项用），null = 按课程概率随机 */
    this.forceLying = null;
    /** 强制体型缩放（对照实验用），null = 按 randomize 随机 / 1.0 */
    this.forceScale = null;
    /** 根节点水平速度（m/s，对照实验用）：模拟浏览器里角色被行走系统平移 */
    this.rootSpeed = 0;
    /** 地形难度 0~1：起伏幅度与特征强度 */
    this.difficulty = 1;
  }

  async init() {
    this.RAPIER = await loadRapier();
  }

  /** 建地形：Minecraft 式体素方块高度场，每局重新生成（地形本身也是随机变量） */
  buildTerrain(RAPIER, world, rnd) {
    const kind = this.forceTerrain === null ? null : this.forceTerrain;
    this.voxel = new VoxelTerrain(rnd, kind, this.difficulty, this.rd.getGroundY());
    this.voxel.build(RAPIER, world, this.rd.getTuning().groundFriction);
    this.terrain = this.voxel.kind;
  }

  reset(seed = 0) {
    const rnd = mulberry32(seed >>> 0);
    this.rng = rnd;
    if (this.rd) { this.rd.dispose(); this.rd = null; }
    this.voxel = null;

    const R = this.randomize;
    // 前端把模型统一缩放到 2.2 高（03_model_load_gltf_vrm.ts），所以骨架 scale 的
    // 基准是 2.2/原始身高 ≈ 1.371，不是 1.0 —— 训练跑 1.0 等于腿比前端短 27%。
    const frontScale = 2.2 / BASE_HEIGHT;
    const scale = this.forceScale !== null
      ? this.forceScale
      : (R ? frontScale * (0.85 + rnd() * 0.35) : frontScale);
    const { bones, root, rig } = buildRig(scale);
    this.bones = bones;
    this.root = root;
    this.standRootQ = root.quaternion.clone();
    this.standRootP = root.position.clone();
    this.pose = snapshotPose(bones);

    // 一半的局从「躺在地上」开始：物理初始位姿躺下，但动画目标仍是站立姿态。
    // PD 面对这种局面只会抽搯 —— 它没有「先翻身再摔起」这种时序概念，
    // 这就是策略真正要学的东西。
    // 课程：难度低时以站立开局为主，先学会站稳，再逐步加大「躺着起身」的比例。
    const lying = this.forceLying !== null
      ? this.forceLying
      : (R ? rnd() < 0.15 + 0.35 * this.difficulty : false);
    if (lying) {
      const kind = Math.floor(rnd() * 3);
      const yaw = rnd() * Math.PI * 2;
      if (kind === 0) root.rotation.set(Math.PI / 2, yaw, 0, 'YXZ');
      else if (kind === 1) root.rotation.set(-Math.PI / 2, yaw, 0, 'YXZ');
      else root.rotation.set(0, yaw, Math.PI / 2, 'YXZ');
      root.position.set(0, 0.32, 0);
    }
    root.updateWorldMatrix(true, true);
    this.startLying = lying;

    const tuning = {
      gravityScale: R ? 0.9 + rnd() * 0.2 : 1,
      follow: R ? 0.75 + rnd() * 0.5 : 1,
      balance: R ? rnd() * 0.35 : 0.0,
      groundFriction: R ? 0.35 + rnd() * 0.85 : 1.0,
      // 力矩上限是「PD 能不能自己站住」的总开关：40 N·m/kg 相当于 4700 N·m 的
      // 髋力矩，把人锁成雕像，推不倒也就没策略什么事。真人髋关节约 1.7 N·m/kg，
      // 这里取 1.0~3.0 —— 刚好卡在「能站、但被推就踉跄」的区间。
      maxTorquePerKg: R ? 1.0 + rnd() * 2.0 : 1.8,
      // 关掉内置的「倒下冻结目标」——那是手工作弊的起身逻辑，策略要自己学起身
      downedHeightRatio: 0,
      subSteps: 1,
      // 内置大地面必须关：它是一块 30×30 的平板，会把地形里所有坑填平
      groundEnabled: false,
    };
    this.rd = new ActiveRagdoll(this.RAPIER, rig, tuning);
    // 站立髋高固定用骨架基准值：倒地开局时从骨骼量会得到躺姿的髋高
    this.standHipY = STAND_HIP_Y * scale;
    this.tuning = tuning;
    // 物理建好后把骨架（动画目标）放回站立位，与躺着不动的物理体形成「目标−现状」张力
    root.quaternion.copy(this.standRootQ);
    root.position.copy(this.standRootP);

    const world = this.rd.getWorld();
    this.buildTerrain(this.RAPIER, world, rnd);
    // 观测里的髋高比要按「支撑面」算，不是髋部正下方的格子：一只脚踩在 40cm 台上、
    // 髋部悬在台外时，髋部正下方是低格，会把台阶高度误算成自己长高
    this.supportY = this.voxel.heightAt(0, 0);
    this.rd.setGroundProbe(() => this.supportY);
    // 地形探针走另一条钩子：groundProbe 返回的是「支撑面」（三点取最高、与坐标
    // 无关），观测要的是按坐标查的地形高度 —— 混用会让探针恒等于支撑面、全为 0
    this.rd.setTerrainProbe((x, z) => this.voxel.heightAt(x, z));

    const parts = this.rd.getParts();

    // 初始姿态扰动：给每个刚体一个小随机朝向偏移，物理从「没站正」开始
    if (R) {
      const jitter = 0.06 * rnd();
      for (const p of parts) {
        const r = p.body.rotation();
        const ax = rnd() - 0.5, ay = rnd() - 0.5, az = rnd() - 0.5;
        const len = Math.hypot(ax, ay, az) || 1;
        const h = jitter / 2 / len;
        const dq = { x: ax * h, y: ay * h, z: az * h, w: 1 };
        const nw = Math.hypot(dq.x, dq.y, dq.z, dq.w);
        const q = {
          x: (r.w * dq.x + r.x * dq.w + r.y * dq.z - r.z * dq.y) / nw,
          y: (r.w * dq.y - r.x * dq.z + r.y * dq.w + r.z * dq.x) / nw,
          z: (r.w * dq.z + r.x * dq.y - r.y * dq.x + r.z * dq.w) / nw,
          w: (r.w * dq.w - r.x * dq.x - r.y * dq.y - r.z * dq.z) / nw,
        };
        p.body.setRotation(q, true);
        p.body.setLinvel({ x: (rnd() - 0.5) * 0.4, y: 0, z: (rnd() - 0.5) * 0.4 }, true);
        p.body.setAngvel({ x: (rnd() - 0.5) * 0.5, y: (rnd() - 0.5) * 0.5, z: (rnd() - 0.5) * 0.5 }, true);
      }
    }

    this.pose = snapshotPose(bones);
    this.t = 0;
    // 摆动频率与相位每局不同
    this.swayRate = 2 * Math.PI / (1.4 + rnd() * 1.2);
    this.swayPhase = rnd() * Math.PI * 2;
    this.swayScale = 0.6 + rnd() * 0.8;
    // 腿部幅度独立随机（5°~26°）：前端动画的腿摆幅度随动作变化，训练里
    // 必须见到整个范围，否则又掉回「只见过 2° 目标」的分布外陷阱。
    this.legSwayScale = 0.3 + rnd() * 1.1;
    this.prevAct.fill(0);
    this.action.fill(0);
    this.pushTimer = R ? 0.4 + rnd() * 1.2 : 1e9;
    this.pushStrength = 0;
    this.pushDir = { x: 0, z: 0 };
    this.rootTravel = { x: 0, z: 0 };

    applyPose(this.bones, this.pose, this.root);
    this.rd.policyResidual = null;
    this.updateSupportY();
    buildObservation(this.rd, this.obs, 0, this.standHipY, 0, 0);
    for (let i = 0; i < ACT_DIM; i++) this.obs[OBS_DIM + i] = 0;
    return this.obs;
  }

  /** 随机瞬时扰动：像被人推一把、被东西撞一下 —— 脉冲式比持续力难对付得多 */
  applyPush() {
    const rnd = this.rng;
    this.pushTimer -= this.dt;
    if (this.pushTimer > 0) return;
    this.pushTimer = 0.5 + rnd() * 1.5;
    const strength = rnd() < 0.3 ? 0 : 0.3 + rnd() * 2.0;
    if (strength <= 0) return;
    const a = rnd() * Math.PI * 2;
    this.rd.push(strength, Math.cos(a), Math.sin(a));
  }

  /**
   * 支撑面高度：髋 + 双脚三点地面取最高 —— 脚踩在台上时台面才是支撑面。
   * 只取髋部正下方会把「站在台阶边缘」算成腾空，奖励凭空变高。
   */
  updateSupportY() {
    let y = this.voxel ? this.voxel.groundY : this.rd.getGroundY();
    if (!this.voxel) { this.supportY = y; return; }
    for (const p of this.rd.getParts()) {
      const b = p.def.bone;
      if (b !== 'hips' && b !== 'leftFoot' && b !== 'rightFoot') continue;
      const t = p.body.translation();
      const h = this.voxel.heightAt(t.x, t.z);
      if (h > y) y = h;
    }
    this.supportY = y;
  }

  /** 恢复基准姿态并叠加上程序化 idle 摆动，得到本帧的动画目标 */
  restorePose() {
    const bones = this.bones, pose = this.pose;
    this.root.quaternion.copy(this.standRootQ);
    this.root.position.copy(this.standRootP);
    if (this.rootSpeed) {
      // 模拟浏览器里角色被行走系统平移：动画目标整体前移，物理只能靠髋弹簧和地面摩擦跟。
      // root 一动，髋的 driveP 每帧就带上一个速度目标、脚被地面拖住 —— 静止训练环境里
      // 完全缺失这一项输入，这正是「前端腿张开比仿真大」的候选来源。
      this.rootTravel.x += this.rootSpeed * this.dt;
      this.root.position.x += this.rootTravel.x;
    }
    for (const name in pose) {
      const b = bones[name];
      b.position.copy(pose[name].p);
      b.quaternion.copy(pose[name].q);
    }
    const ph = this.t * this.swayRate + this.swayPhase;
    for (const s of SWAY) {
      const b = bones[s.bone];
      if (!b) continue;
      _swayAxis.set(s.axis[0], s.axis[1], s.axis[2]);
      const sc = s.leg ? this.legSwayScale : this.swayScale;
      _swayQ.setFromAxisAngle(_swayAxis, s.amp * sc * Math.sin(ph * s.freq));
      b.quaternion.multiply(_swayQ);
    }
    this.root.updateWorldMatrix(true, true);
  }

  /**
   * 走一步。action 为未限幅的网络输出（写进物理时自动限幅）。
   * @returns {{ reward:number, done:boolean, info:object }}
   */
  step(action) {
    this.updateSupportY();
    writeAction(this.rd, action);
    this.restorePose();
    this.applyPush();
    this.rd.update(this.dt);
    this.t++;

    const parts = this.rd.getParts();
    const hip = parts[0];
    const t = hip.body.translation();
    const hipH = t.y - this.rd.groundHeightAt(t.x, t.z);
    const hipRatio = hipH / this.standHipY;

    // 躯干直立度：髋段局部 +Y 在世界里的方向
    const r = hip.body.rotation();
    const upX = 2 * (r.x * r.y - r.w * r.z);
    const upY = 1 - 2 * (r.x * r.x + r.z * r.z);
    const upZ = 2 * (r.y * r.z + r.w * r.x);

    const timeRatio = this.t / this.episodeSteps;
    buildObservation(this.rd, this.obs, 0, this.standHipY, this.t * this.swayRate + this.swayPhase, timeRatio);
    // 奖励口径必须用「原始动画目标 targetQ」，不能用 driveQ：策略的输出会改写
    // driveQ，若按 driveQ 算误差，策略只要把目标转到自己身上就能白拿奖励 ——
    // 实测 4 万步就会退化成「目标追着身体跑」的作弊解。
    let angErr = 0;
    for (const p of parts) {
      const rr = p.body.rotation();
      _qA.set(rr.x, rr.y, rr.z, rr.w);
      _qB.copy(p.targetQ).multiply(_qC.copy(_qA).invert());
      const w = Math.min(1, Math.abs(_qB.w));
      const ang = 2 * Math.acos(w);
      angErr += ang * ang;
    }
    angErr /= parts.length;

    let actSq = 0, smoothSq = 0;
    for (let i = 0; i < ACT_DIM; i++) {
      const a = action[i];
      actSq += a * a;
      const d = a - this.prevAct[i];
      smoothSq += d * d;
    }

    let reward = 0.6;
    const hipClamp = Math.max(0, Math.min(1, hipRatio));
    reward += 2.5 * hipClamp;
    reward += 0.5 * Math.max(0, Math.min(1, (upY - 0.5) * 2));
    if (this.startLying) {
      // 躺姿局追加：上面那项 (upY-0.5)*2 被截断到 0，仰躺/侧躺/趴着同分，策略
      // 拿不到「往哪边翻」的方向。换成不截断的 (upY+1)/2，并和髋高一起加倍权重
      // —— 躺在地上时这两项是指向「站起来」的唯一梯度。站姿局逐位不变，
      // 免得把已经追平 PD 的站姿一起改掉、失去可比性。
      reward += 2.0 * Math.max(0, Math.min(1, (upY + 1) * 0.5));
      reward += 1.5 * hipClamp;
    }
    reward -= 0.6 * angErr;
    reward -= 0.02 * smoothSq;
    reward -= 0.001 * actSq;

    // 不提前终止：躺在地上是合法状态，只是回报低 —— 提前终止会让「刚开始学起身
    // 就摔倒」的样本直接消失，梯度信号反而变少
    const done = this.t >= this.episodeSteps;
    const info = { hipRatio, upY, angErr, terrain: this.terrain, balance: this.tuning.balance, lying: this.startLying };

    for (let i = 0; i < ACT_DIM; i++) {
      const v = action[i];
      this.prevAct[i] = v > 0.6 ? 0.6 : v < -0.6 ? -0.6 : v;
      this.obs[OBS_DIM + i] = this.prevAct[i];
    }
    return { reward, done, info };
  }

  /** 不训练、纯跑策略做评估：返回统计 */
  rollout(policyFn, seed, maxSteps = this.episodeSteps) {
    let obs = this.reset(seed);
    let steps = 0, ret = 0, minHip = 1e9, sumAng = 0;
    for (let i = 0; i < maxSteps; i++) {
      const act = policyFn(obs);
      const { reward, done, info } = this.step(act);
      ret += reward; steps++;
      minHip = Math.min(minHip, info.hipRatio);
      sumAng += info.angErr;
      obs = this.obs;
      if (done) break;
    }
    return { steps, ret, minHip, meanAng: sumAng / steps, survived: steps === maxSteps };
  }

  dispose() {
    if (this.rd) { this.rd.dispose(); this.rd = null; }
  }
}
