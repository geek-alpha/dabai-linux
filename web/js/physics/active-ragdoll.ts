/* ============================================================
 * 主动布娃娃（Active Ragdoll）—— 物理身体层
 * ------------------------------------------------------------
 * 给 VRM 角色一副真身体：每段骨骼 = 一个刚体胶囊，关节用球形约束
 * 连成链，重力、摩擦、碰撞全部交给 rapier 算。每帧做两件事：
 *
 *   1. 从「动画姿态」读目标位姿（raw 骨骼的世界四元数/位置）
 *   2. 用 PD 扭矩把刚体往目标上拉，步进物理，再把物理位姿写回骨骼
 *
 * 物理状态跨帧保留、目标每帧重取 —— 所以推一把会踉跄、会被撞倒、
 * 会在地上躺一会再爬起来；跟随强度（follow）与髋部支撑（balance）
 * 决定「像动画」还是「像物理」。
 *
 * balance=0 时没有任何外力托住身体，站不站得住完全取决于有没有
 * 学出来的控制策略 —— 这就是后面接 RL / ONNX 策略的插口：
 * 把 applyDrive 里的髋部弹簧换成策略网络输出即可。
 *
 * 世界空间约定：物理在世界坐标里算，写回时再换算到骨骼局部空间，
 * 因此移动模式里缩放/平移角色不会让物理错位。
 * ============================================================ */

import * as THREE from 'three';
import type * as RAPIERNS from '@dimforge/rapier3d-compat';
import { DEFAULT_TUNING, SEGMENTS, scaleFor } from './ragdoll-config.js';
import type { RagdollTuning, SegmentDef } from './ragdoll-config.js';

export type RapierModule = typeof RAPIERNS;

let _rapier: RapierModule | null = null;
let _rapierLoading: Promise<RapierModule> | null = null;

/** 懒加载 rapier（wasm 内联在 mjs 里，首次调用约 200~400ms） */
export function loadRapier(): Promise<RapierModule> {
  if (_rapier) return Promise.resolve(_rapier);
  if (!_rapierLoading) {
    _rapierLoading = import('@dimforge/rapier3d-compat').then(async (mod) => {
      const RAPIER = (mod as any).default ?? mod;
      await RAPIER.init();
      _rapier = RAPIER as RapierModule;
      return _rapier;
    }).catch((e) => {
      _rapierLoading = null;
      throw e;
    });
  }
  return _rapierLoading;
}

/** 从 VRM 收集物理所需的骨骼与尺度 */
export interface RagdollRig {
  /** 骨骼名 → 节点（raw 骨骼优先，缺则退 normalized） */
  bones: Record<string, THREE.Object3D>;
  /** 模型身高（世界单位，米） */
  height: number;
  /** 脚底所在世界高度（地面） */
  groundY: number;
}

export function collectRig(vrm: any): RagdollRig | null {
  const humanoid = vrm?.humanoid;
  if (!humanoid) return null;
  const bones: Record<string, THREE.Object3D> = {};
  for (const def of SEGMENTS) {
    const node = (humanoid.getRawBoneNode?.(def.bone)) || humanoid.getNormalizedBoneNode?.(def.bone);
    if (!node) return null;
    bones[def.bone] = node;
  }
  const root: THREE.Object3D | undefined = vrm.scene;
  if (!root) return null;
  root.updateWorldMatrix(true, true);
  const box = new THREE.Box3().setFromObject(root);
  const size = new THREE.Vector3();
  box.getSize(size);
  const height = size.y > 0.2 && isFinite(size.y) ? size.y : 1.7;
  return { bones, height, groundY: box.min.y };
}

export interface Part {
  def: SegmentDef;
  bone: THREE.Object3D;
  body: RAPIERNS.RigidBody;
  parentIndex: number;
  /** 静息相对旋转（相对父体局部），用于软限位基准 */
  restRelQ: THREE.Quaternion;
  /** 动画目标（每帧从骨骼读） */
  targetQ: THREE.Quaternion;
  targetP: THREE.Vector3;
  /** 实际驱动目标（倒下时冻结，起身时插值回动画目标） */
  driveQ: THREE.Quaternion;
  driveP: THREE.Vector3;
  kp: number;
  kd: number;
  maxTorque: number;
  maxDeviationCos: number;
  hasParent: boolean;
  /** 脚掌贴地球（仅 Foot 段）：球心在身体局部坐标 + 半径 + 绑定姿态踝高，屏上诊断用 */
  sole?: { x: number; y: number; z: number; r: number; bindGap: number };
}

export interface RagdollState {
  enabled: boolean;
  bodies: number;
  hipHeight: number;
  speed: number;
  downed: boolean;
  /** 上一帧物理步进耗时（ms） */
  stepMs: number;
}

const _v1 = new THREE.Vector3();
const _v2 = new THREE.Vector3();
const _v3 = new THREE.Vector3();
const _q1 = new THREE.Quaternion();
const _q2 = new THREE.Quaternion();
const _q3 = new THREE.Quaternion();
const _m1 = new THREE.Matrix4();

export class ActiveRagdoll {
  private RAPIER: RapierModule;
  private rig: RagdollRig;
  private tuning: RagdollTuning;
  private world: RAPIERNS.World;
  private parts: Part[] = [];
  private jointHandles: RAPIERNS.ImpulseJoint[] = [];
  private totalMass = 0;
  private groundY: number;
  private scale: number;
  /** 局部地面高度查询钩子：训练时指向体素地形，浏览器不设 → 恒为 groundY */
  private groundProbe: ((x: number, z: number) => number) | null = null;
  /** 地形高度查询钩子（真按 x/z 查）：与 groundProbe 分开 —— 后者是「支撑面」，忽略坐标 */
  private terrainProbe: ((x: number, z: number) => number) | null = null;

  private downed = false;
  private downTimer = 0;
  private recoverT = 0;
  private stepMs = 0;
  private lastSpeed = 0;

  /** 策略残差：每段 3 维旋转向量（世界系，弧度），叠加到驱动目标上。null = 纯 PD 控制 */
  policyResidual: Float32Array | null = null;

  constructor(RAPIER: RapierModule, rig: RagdollRig, tuning?: Partial<RagdollTuning>) {
    this.RAPIER = RAPIER;
    this.rig = rig;
    this.tuning = { ...DEFAULT_TUNING, ...(tuning || {}) };
    this.scale = scaleFor(rig.height);
    this.groundY = rig.groundY;
    this.world = new RAPIER.World({ x: 0, y: -9.81 * this.tuning.gravityScale, z: 0 });
    this.world.numSolverIterations = 8;
    this.build();
  }

  /* ---------------- 构建 ---------------- */

  private build() {
    const RAPIER = this.RAPIER;
    const scale = this.scale;

    // 地面：大而厚的固定方块，顶面对齐脚底高度。训练侧关掉它，因为起伏地形
    // 需要向下挖坑 —— 这块 30×30 的平板会把所有坑填平。
    if (this.tuning.groundEnabled) {
      const groundBody = this.world.createRigidBody(
        RAPIER.RigidBodyDesc.fixed().setTranslation(0, this.groundY - 0.5, 0)
      );
      this.world.createCollider(
        RAPIER.ColliderDesc.cuboid(30, 0.5, 30).setFriction(this.tuning.groundFriction),
        groundBody
      );
    }

    // 先按段定义建体：体位姿直接取骨骼世界位姿（VRM 骨骼局部 +Y 指向子骨骼，
    // 所以骨骼世界四元数天然就是「段轴对齐」的体旋转，不用另外算朝向）
    for (const def of SEGMENTS) {
      const bone = this.rig.bones[def.bone];
      const p = bone.getWorldPosition(new THREE.Vector3());
      const q = bone.getWorldQuaternion(new THREE.Quaternion());
      const mass = def.mass * scale * scale * scale;
      this.totalMass += mass;

      const body = this.world.createRigidBody(
        RAPIER.RigidBodyDesc.dynamic()
          .setTranslation(p.x, p.y, p.z)
          .setRotation({ x: q.x, y: q.y, z: q.z, w: q.w })
          .setLinearDamping(0.05)
          .setAngularDamping(0.35)
          .setCanSleep(false)
          // 高速穿透防护：被推飞时单帧位移可能超过地面厚度（实测 25 倍推力下 84m/s ≈ 1.4m/帧）
          .setCcdEnabled(true)
      );

      const child = def.child ? this.rig.bones[def.child] : null;
      let len = def.fixedLen ? def.fixedLen * scale : 0;
      if (child) {
        const cp = child.getWorldPosition(_v1);
        len = cp.distanceTo(p);
      }
      if (!(len > 0.01)) len = 0.1 * scale;
      const radius = Math.max(0.02, def.radius * scale);

      let colliderDesc: RAPIERNS.ColliderDesc;
      if (def.ballOffsetY !== undefined) {
        // 头：球体，沿段轴抬一点，避免和脖子胶囊重叠
        colliderDesc = RAPIER.ColliderDesc.ball(radius).setTranslation(0, def.ballOffsetY * scale, 0);
      } else {
        // 胶囊沿自身 +Y 建，但「骨骼局部 +Y 指向子骨骼」只是部分模型的约定：
        // 白头凤的腿骨子节点在 -Y、上臂在 -X，按 +Y 硬建会把胶囊长到膝盖上方、
        // 手臂竖起来（实测就是「腿歪 + 悬空」）。这里把段方向换算到骨骼局部空间再对齐。
        const half = Math.max(0.01, len / 2 - radius);
        const dir = _v3.set(0, 1, 0);
        if (child) {
          dir.copy(child.getWorldPosition(_v1)).sub(p);
          if (dir.lengthSq() > 1e-9) dir.normalize().applyQuaternion(_q1.copy(q).invert());
          else dir.set(0, 1, 0);
        }
        _q2.setFromUnitVectors(_v1.set(0, 1, 0), dir);
        colliderDesc = RAPIER.ColliderDesc.capsule(half, radius)
          .setTranslation((dir.x * len) / 2, (dir.y * len) / 2, (dir.z * len) / 2)
          .setRotation({ x: _q2.x, y: _q2.y, z: _q2.z, w: _q2.w });
      }
      colliderDesc.setMass(mass).setFriction(0.9).setRestitution(0.02);
      this.world.createCollider(colliderDesc, body);

      // 脚掌：踝骨长在脚底上方约 12cm，只沿段轴的胶囊永远够不到地面 —— 身体
      // 全靠髋部弹簧吊着，视觉上就是「腿悬空」（实测脚离地 7.8cm）。在踝下方
      // 补一个球把脚踩实：球心在踝下 (踝高 - r)，球底正好落在 groundY。
      let sole: Part['sole'];
      if (def.bone.endsWith('Foot')) {
        const drop = p.y - this.groundY - radius;
        if (drop > 0) {
          _v2.set(0, -drop, 0).applyQuaternion(_q1.copy(q).invert());
          this.world.createCollider(
            RAPIER.ColliderDesc.ball(radius)
              .setTranslation(_v2.x, _v2.y, _v2.z)
              .setMass(mass * 0.4).setFriction(0.9).setRestitution(0.02),
            body
          );
          sole = { x: _v2.x, y: _v2.y, z: _v2.z, r: radius, bindGap: p.y - this.groundY };
        }
      }

      const parentIndex = def.parent ? SEGMENTS.findIndex((s) => s.bone === def.parent) : -1;
      this.parts.push({
        def,
        bone,
        body,
        parentIndex,
        restRelQ: new THREE.Quaternion(),
        targetQ: q.clone(),
        targetP: p.clone(),
        driveQ: q.clone(),
        driveP: p.clone(),
        kp: def.kp * this.tuning.follow,
        kd: def.kd,
        maxTorque: def.mass * scale * scale * scale * 9.81 * this.tuning.maxTorquePerKg,
        maxDeviationCos: Math.cos((def.maxDeviation * Math.PI) / 180),
        hasParent: parentIndex >= 0,
        sole,
      });
    }

    // 关节：锚点用「子段骨骼世界位置」在父体局部空间表达 —— 骨架里这个点
    // 就是父段远端关节（肘/膝/踝），跳过肩等中间骨骼也不会错位。
    // rapier 的 RigidBody 没有 worldToLocal，这里自己换算（刚体无缩放，只差旋转平移）。
    for (let i = 0; i < this.parts.length; i++) {
      const part = this.parts[i];
      if (!part.hasParent) continue;
      const parent = this.parts[part.parentIndex];
      const jointPoint = part.bone.getWorldPosition(new THREE.Vector3());
      const pt = parent.body.translation();
      const pr = parent.body.rotation();
      _q1.set(pr.x, pr.y, pr.z, pr.w).invert();
      _v1.set(jointPoint.x - pt.x, jointPoint.y - pt.y, jointPoint.z - pt.z).applyQuaternion(_q1);
      const data = RAPIER.JointData.spherical(
        { x: _v1.x, y: _v1.y, z: _v1.z },
        { x: 0, y: 0, z: 0 }
      );
      this.jointHandles.push(this.world.createImpulseJoint(data, parent.body, part.body, true));
      // 静息相对旋转：软限位基准（偏离它超过 maxDeviation 才回正）
      const parentQ = parent.bone.getWorldQuaternion(new THREE.Quaternion());
      const selfQ = part.bone.getWorldQuaternion(new THREE.Quaternion());
      part.restRelQ.copy(parentQ).invert().multiply(selfQ);
    }
  }

  /* ---------------- 每帧 ---------------- */

  update(dt: number) {
    if (!this.parts.length) return;
    const t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());

    this.readTargets();
    this.updateDowned(dt);
    this.applyPolicyResidual();
    // 飞出世界兜底：必须放在 writeBack 之前 —— 此刻骨骼仍是干净的动画姿态，复位目标才准。
    // 写在 writeBack 之后会把「已被物理污染的骨骼位置」当复位目标，结果每帧复位到飞出点，回不来（实测）
    if (this.isOutOfWorld()) this.resetToTargets();

    const subSteps = Math.max(1, Math.min(4, this.tuning.subSteps | 0));
    const sub = dt / subSteps;
    this.world.timestep = sub;
    for (let s = 0; s < subSteps; s++) {
      this.applyDrive(sub);
      this.world.step();
    }

    this.writeBack();

    const hip = this.parts[0];
    const v = hip.body.linvel();
    this.lastSpeed = Math.hypot(v.x, v.y, v.z);
    this.stepMs = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
  }

  /** 从动画姿态读目标；倒下期间冻结驱动目标，起身时插值回动画目标 */
  private readTargets() {
    const ramp = this.tuning.recoverRamp > 0 ? 1 / this.tuning.recoverRamp : 1;
    for (const part of this.parts) {
      part.targetQ.copy(part.bone.getWorldQuaternion(_q1));
      if (part.parentIndex < 0) part.targetP.copy(part.bone.getWorldPosition(_v1));

      if (!this.downed) {
        // 正常：驱动目标直接跟动画
        part.driveQ.copy(part.targetQ);
        if (part.parentIndex < 0) part.driveP.copy(part.targetP);
        this.recoverT = 0;
      } else if (this.recoverT > 0) {
        // 起身：从躺姿插值回动画姿态
        part.driveQ.slerp(part.targetQ, ramp);
        if (part.parentIndex < 0) part.driveP.lerp(part.targetP, ramp);
      }
      // downed 且未进入起身：driveQ/driveP 保持不变 = 保持躺姿
    }
  }

  /** 倒下判定与起身节奏：髋高掉到站立髋高的 62% 以下算倒 */
  private updateDowned(dt: number) {
    const hip = this.parts[0];
    const hipY = hip.body.translation().y;
    const standHipY = hip.targetP.y;
    const fallY = this.groundY + (standHipY - this.groundY) * this.tuning.downedHeightRatio;
    const low = hipY < fallY && this.lastSpeed < 1.5;

    if (!this.downed) {
      if (low) {
        this.downTimer += dt;
        if (this.downTimer > 0.5) {
          this.downed = true;
          this.downTimer = 0;
          this.recoverT = 0;
        }
      } else {
        this.downTimer = 0;
      }
      return;
    }

    // 已倒下：躺 recoverDelay 秒后开始起身
    if (this.recoverT === 0) {
      this.downTimer += dt;
      if (this.downTimer > this.tuning.recoverDelay) {
        this.downTimer = 0;
        this.recoverT = 1e-6;
      }
      return;
    }

    // 起身过渡：目标已插值回动画姿态，髋部支撑同步回升（平衡辅助 ramp 到 1）
    this.recoverT += dt;
    if (this.recoverT > this.tuning.recoverRamp && hipY > fallY + 0.05) {
      this.downed = false;
      this.recoverT = 0;
      this.downTimer = 0;
    }
  }

  /** PD 驱动：角向扭矩拉姿态 + 髋部线性弹簧托住身体（平衡辅助） */
  private applyDrive(dt: number) {
    const recoverFactor = this.downed ? Math.min(1, this.recoverT / Math.max(1e-3, this.tuning.recoverRamp)) : 1;
    const balance = this.tuning.balance * recoverFactor;

    for (const part of this.parts) {
      const body = part.body;
      const rot = body.rotation();
      _q1.set(rot.x, rot.y, rot.z, rot.w);

      // 世界空间角误差（旋转向量）：qErr = target * current⁻¹
      _q2.copy(part.driveQ).multiply(_q3.copy(_q1).invert());
      if (_q2.w < 0) _q2.set(-_q2.x, -_q2.y, -_q2.z, -_q2.w);
      const w = Math.min(1, Math.max(-1, _q2.w));
      const angle = 2 * Math.acos(w);
      const sinHalf = Math.sqrt(Math.max(0, 1 - w * w));
      let ex = 0, ey = 0, ez = 0;
      if (sinHalf > 1e-5 && angle > 1e-5) {
        const k = angle / sinHalf;
        ex = _q2.x * k; ey = _q2.y * k; ez = _q2.z * k;
      }

      const omega = body.angvel();
      let tx = part.kp * ex - part.kd * omega.x;
      let ty = part.kp * ey - part.kd * omega.y;
      let tz = part.kp * ez - part.kd * omega.z;

      // 软限位：相对父体偏离静息姿态超过 maxDeviation 就回正。
      // rapier JS 侧的球形关节只暴露单对 [min,max] 标量限位，对 3 自由度关节
      // 语义不明确，所以在 PD 层做软限位（X = 静息相对旋转 × 实际相对旋转⁻¹，
      // 在父体坐标系里，最后用父体世界旋转转到世界系）
      if (part.hasParent) {
        const parent = this.parts[part.parentIndex];
        const pr = parent.body.rotation();
        _q2.set(pr.x, pr.y, pr.z, pr.w);
        _q3.copy(_q2).invert().multiply(_q1); // relQ = inv(parent) * self
        _q1.copy(part.restRelQ).multiply(_q3.invert()); // X = rest * inv(rel)，父体系
        if (_q1.w < 0) _q1.set(-_q1.x, -_q1.y, -_q1.z, -_q1.w);
        const lw = Math.min(1, _q1.w);
        const devAngle = 2 * Math.acos(lw);
        const devLimit = Math.acos(Math.min(1, Math.max(-1, part.maxDeviationCos)));
        const s = Math.sqrt(Math.max(0, 1 - lw * lw));
        if (devAngle > devLimit && s > 1e-5) {
          const over = devAngle - devLimit;
          _v1.set(_q1.x / s, _q1.y / s, _q1.z / s).applyQuaternion(_q2); // 父体系 → 世界系
          const k = over * part.kp * 0.5;
          tx += _v1.x * k; ty += _v1.y * k; tz += _v1.z * k;
        }
      }

      const maxT = part.maxTorque;
      const tLen = Math.hypot(tx, ty, tz);
      if (tLen > maxT) {
        const k = maxT / tLen;
        tx *= k; ty *= k; tz *= k;
      }
      body.applyTorqueImpulse({ x: tx * dt, y: ty * dt, z: tz * dt }, true);

      // 髋部（根段）：线性弹簧托住身体。balance=0 就是纯物理，会倒
      if (!part.hasParent && balance > 0) {
        const t = body.translation();
        const v = body.linvel();
        const m = body.mass();
        const ax = this.tuning.hipKp * (part.driveP.x - t.x) - this.tuning.hipKd * v.x;
        const ay = this.tuning.hipKp * (part.driveP.y - t.y) - this.tuning.hipKd * v.y;
        const az = this.tuning.hipKp * (part.driveP.z - t.z) - this.tuning.hipKd * v.z;
        body.applyImpulse({ x: m * ax * balance * dt, y: m * ay * balance * dt, z: m * az * balance * dt }, true);
      }
    }
  }

  /** 物理位姿写回骨骼：髋写位置+旋转，其余只写旋转（保持骨架长度，不拉长肢体） */
  private writeBack() {
    for (const part of this.parts) {
      const t = part.body.translation();
      const r = part.body.rotation();
      _q1.set(r.x, r.y, r.z, r.w);
      const parent = part.bone.parent;
      if (parent) {
        parent.getWorldQuaternion(_q2).invert();
        _q3.copy(_q2).multiply(_q1);
        part.bone.quaternion.copy(_q3);
      } else {
        part.bone.quaternion.copy(_q1);
      }
      if (!part.hasParent) {
        if (parent) {
          _v2.set(t.x, t.y, t.z).applyMatrix4(_m1.copy(parent.matrixWorld).invert());
          part.bone.position.copy(_v2);
        } else {
          part.bone.position.set(t.x, t.y, t.z);
        }
      }
    }
  }

  /** 掉出世界兜底：被推飞或数值发散时把身体搬回动画姿态，别让角色消失在虚空里。
   *  水平判据用「距动画髋目标的距离」而不是绝对坐标 —— 角色被摆在场景任何位置都不误判。 */
  private isOutOfWorld(): boolean {
    const part = this.parts[0];
    if (!part) return false;
    const t = part.body.translation();
    if (!isFinite(t.x) || !isFinite(t.y) || !isFinite(t.z)) return true;
    if (t.y < this.groundY - 3) return true;
    const dx = t.x - part.targetP.x;
    const dz = t.z - part.targetP.z;
    return dx * dx + dz * dz > 100;
  }

  /** 复位目标取自骨骼世界位姿 —— 调用时机必须在 writeBack 之前（骨骼尚未被物理写回污染） */
  private resetToTargets() {
    for (const part of this.parts) {
      const p = part.bone.getWorldPosition(_v1);
      const q = part.bone.getWorldQuaternion(_q1);
      part.body.setTranslation({ x: p.x, y: p.y, z: p.z }, true);
      part.body.setRotation({ x: q.x, y: q.y, z: q.z, w: q.w }, true);
      part.body.setLinvel({ x: 0, y: 0, z: 0 }, true);
      part.body.setAngvel({ x: 0, y: 0, z: 0 }, true);
      part.driveQ.copy(part.targetQ);
      part.driveP.copy(part.targetP);
    }
    this.downed = false;
    this.downTimer = 0;
    this.recoverT = 0;
    this.lastSpeed = 0;
  }

  /* ---------------- 外部接口 ---------------- */

  /** 把策略残差叠加到驱动目标上：世界系左乘，零动作 = 原 PD 行为 */
  private applyPolicyResidual() {
    const res = this.policyResidual;
    if (!res) return;
    const n = Math.min(this.parts.length, (res.length / 3) | 0);
    for (let i = 0; i < n; i++) {
      const o = i * 3;
      const rx = res[o], ry = res[o + 1], rz = res[o + 2];
      if (rx === 0 && ry === 0 && rz === 0) continue;
      const ang = Math.sqrt(rx * rx + ry * ry + rz * rz);
      // 轴角 → 四元数；ang→0 时 sin(ang/2)/ang → 1/2，避免除零
      const s = ang > 1e-6 ? Math.sin(ang / 2) / ang : 0.5;
      _q1.set(rx * s, ry * s, rz * s, Math.cos(ang / 2));
      this.parts[i].driveQ.premultiply(_q1);
    }
  }

  /** 只读访问：策略观测/训练环境需要遍历物理段 */
  getParts(): readonly Part[] { return this.parts; }
  getRapier(): RapierModule { return this.RAPIER; }
  getWorld(): RAPIERNS.World { return this.world; }
  getGroundY(): number { return this.groundY; }

  /**
   * 设局部地面高度查询（起伏地形用）。观测里的「髋高比」是相对脚下地面的
   * 量，不是相对某块基准平板 —— 站在 40cm 高的台阶上时，前者才是角色能
   * 感知到的本体信息，后者会把台阶高度误当成「自己长高了」。
   */
  setGroundProbe(fn: ((x: number, z: number) => number) | null): void {
    this.groundProbe = fn;
  }

  /** 点 (x,z) 处的地面高度：有 probe 就问它，否则就是内置平板高度 */
  groundHeightAt(x: number, z: number): number {
    return this.groundProbe ? this.groundProbe(x, z) : this.groundY;
  }

  /**
   * 设地形高度查询（按坐标，观测探针用）。与 setGroundProbe 的区别：那个返回
   * 「支撑面高度」（髋+双脚三点取最高，与坐标无关），是奖励和髋高比的基准；
   * 观测要的是「身前那格有多高」，必须真按坐标查，否则探针恒等于支撑面。
   */
  setTerrainProbe(fn: ((x: number, z: number) => number) | null): void {
    this.terrainProbe = fn;
  }

  /** 点 (x,z) 处的地形高度：无 probe（平地）时就是基准地面 */
  terrainHeightAt(x: number, z: number): number {
    return this.terrainProbe ? this.terrainProbe(x, z) : this.groundY;
  }
  getScale(): number { return this.scale; }
  getTotalMass(): number { return this.totalMass; }
  getDowned(): boolean { return this.downed; }

  /** 推一把：水平冲量，强度 1 ≈ 让 70kg 的人踉跄一步 */
  push(strength = 1, dirX = 0, dirZ = -1) {
    const len = Math.hypot(dirX, dirZ) || 1;
    const impulse = 260 * strength * this.totalMass / 71;
    for (const part of this.parts) {
      const w = part.parentIndex < 0 ? 0.5 : (part.def.bone === 'chest' ? 0.5 : 0);
      if (w <= 0) continue;
      part.body.applyImpulse(
        { x: (dirX / len) * impulse * w, y: 0, z: (dirZ / len) * impulse * w },
        true
      );
    }
  }

  setTuning(patch: Partial<RagdollTuning>) {
    Object.assign(this.tuning, patch);
    const scale = this.scale;
    this.world.gravity = { x: 0, y: -9.81 * this.tuning.gravityScale, z: 0 };
    for (const part of this.parts) {
      part.kp = part.def.kp * this.tuning.follow;
      // maxTorque 是构造时按当时的 maxTorquePerKg 算死的，不在这里重算就是空转：
      // 策略模式传 maxTorquePerKg=2.0 而身体仍按默认 40 跑，扭矩差 20 倍，实测腿被
      // 超强扭矩拽着在空中乱甩、不落地 —— 看起来像「被控制住」，其实是在训练分布外。
      part.maxTorque = part.def.mass * scale * scale * scale * 9.81 * this.tuning.maxTorquePerKg;
    }
    // 地面摩擦改了要重建碰撞体才生效，代价大 —— 只改重力/增益，摩擦下次重建生效
  }

  getTuning(): RagdollTuning {
    return { ...this.tuning };
  }

  /** 屏上诊断快照：手机端没有控制台，这些量只有画到页面上才拿得到证据 */
  diag() {
    const feet: { bone: string; soleGap: number; bindGap: number }[] = [];
    for (const p of this.parts) {
      if (!p.sole) continue;
      const t = p.body.translation();
      const r = p.body.rotation();
      _q1.set(r.x, r.y, r.z, r.w);
      _v1.set(p.sole.x, p.sole.y, p.sole.z).applyQuaternion(_q1);
      feet.push({ bone: p.def.bone, soleGap: t.y + _v1.y - p.sole.r - this.groundY, bindGap: p.sole.bindGap });
    }
    const hip = this.parts[0];
    return {
      groundY: this.groundY,
      scale: this.scale,
      height: this.rig.height,
      hipY: hip ? hip.body.translation().y : 0,
      standHipY: hip ? hip.targetP.y : 0,
      feet,
    };
  }

  getState(): RagdollState {
    const hip = this.parts[0];
    return {
      enabled: true,
      bodies: this.parts.length,
      hipHeight: hip ? hip.body.translation().y - this.groundY : 0,
      speed: this.lastSpeed,
      downed: this.downed,
      stepMs: this.stepMs,
    };
  }

  /** 释放物理世界（切换模型 / 关掉物理时调用） */
  dispose() {
    for (const j of this.jointHandles) this.world.removeImpulseJoint(j, false);
    this.jointHandles.length = 0;
    this.parts.length = 0;
    this.world.free();
  }
}
