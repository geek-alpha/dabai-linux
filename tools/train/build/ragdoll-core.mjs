import * as THREE from "three";
//#region web/js/physics/ragdoll-config.ts
const HEIGHT_REF = 1.7;
const SEGMENTS = [
	{
		bone: "hips",
		parent: null,
		child: "spine",
		radius: .105,
		mass: 12,
		kp: 1200,
		kd: 80,
		maxDeviation: 100
	},
	{
		bone: "spine",
		parent: "hips",
		child: "chest",
		radius: .11,
		mass: 8,
		kp: 900,
		kd: 60,
		maxDeviation: 40
	},
	{
		bone: "chest",
		parent: "spine",
		child: "neck",
		radius: .115,
		mass: 10,
		kp: 900,
		kd: 60,
		maxDeviation: 40
	},
	{
		bone: "neck",
		parent: "chest",
		child: "head",
		radius: .05,
		mass: 2,
		kp: 400,
		kd: 20,
		maxDeviation: 45
	},
	{
		bone: "head",
		parent: "neck",
		child: null,
		radius: .1,
		mass: 5,
		kp: 300,
		kd: 18,
		maxDeviation: 50,
		ballOffsetY: .03
	},
	{
		bone: "leftUpperArm",
		parent: "chest",
		child: "leftLowerArm",
		radius: .045,
		mass: 2,
		kp: 250,
		kd: 12,
		maxDeviation: 170
	},
	{
		bone: "leftLowerArm",
		parent: "leftUpperArm",
		child: "leftHand",
		radius: .04,
		mass: 1.5,
		kp: 250,
		kd: 12,
		maxDeviation: 150
	},
	{
		bone: "rightUpperArm",
		parent: "chest",
		child: "rightLowerArm",
		radius: .045,
		mass: 2,
		kp: 250,
		kd: 12,
		maxDeviation: 170
	},
	{
		bone: "rightLowerArm",
		parent: "rightUpperArm",
		child: "rightHand",
		radius: .04,
		mass: 1.5,
		kp: 250,
		kd: 12,
		maxDeviation: 150
	},
	{
		bone: "leftUpperLeg",
		parent: "hips",
		child: "leftLowerLeg",
		radius: .075,
		mass: 8,
		kp: 900,
		kd: 55,
		maxDeviation: 100
	},
	{
		bone: "leftLowerLeg",
		parent: "leftUpperLeg",
		child: "leftFoot",
		radius: .06,
		mass: 4,
		kp: 700,
		kd: 45,
		maxDeviation: 150
	},
	{
		bone: "leftFoot",
		parent: "leftLowerLeg",
		child: "leftToes",
		radius: .045,
		mass: 1.5,
		kp: 300,
		kd: 20,
		maxDeviation: 50,
		fixedLen: .14
	},
	{
		bone: "rightUpperLeg",
		parent: "hips",
		child: "rightLowerLeg",
		radius: .075,
		mass: 8,
		kp: 900,
		kd: 55,
		maxDeviation: 100
	},
	{
		bone: "rightLowerLeg",
		parent: "rightUpperLeg",
		child: "rightFoot",
		radius: .06,
		mass: 4,
		kp: 700,
		kd: 45,
		maxDeviation: 150
	},
	{
		bone: "rightFoot",
		parent: "rightLowerLeg",
		child: "rightToes",
		radius: .045,
		mass: 1.5,
		kp: 300,
		kd: 20,
		maxDeviation: 50,
		fixedLen: .14
	}
];
const DEFAULT_TUNING = {
	gravityScale: 1,
	follow: 1,
	balance: .9,
	groundFriction: 1,
	maxTorquePerKg: 40,
	subSteps: 2,
	downedHeightRatio: .62,
	recoverDelay: 1.2,
	recoverRamp: 1.4,
	hipKp: 420,
	hipKd: 42,
	groundEnabled: true
};
/** 身高 → 缩放系数（限制在 0.5~2.0 倍，避免异常模型把物理参数拉到不可用区间） */
function scaleFor(height) {
	const s = height / HEIGHT_REF;
	if (!isFinite(s) || s <= 0) return 1;
	return Math.min(2, Math.max(.5, s));
}
//#endregion
//#region web/js/physics/active-ragdoll.ts
let _rapier = null;
let _rapierLoading = null;
/** 懒加载 rapier（wasm 内联在 mjs 里，首次调用约 200~400ms） */
function loadRapier() {
	if (_rapier) return Promise.resolve(_rapier);
	if (!_rapierLoading) _rapierLoading = import("@dimforge/rapier3d-compat").then(async (mod) => {
		const RAPIER = mod.default ?? mod;
		await RAPIER.init();
		_rapier = RAPIER;
		return _rapier;
	}).catch((e) => {
		_rapierLoading = null;
		throw e;
	});
	return _rapierLoading;
}
function collectRig(vrm) {
	const humanoid = vrm?.humanoid;
	if (!humanoid) return null;
	const bones = {};
	for (const def of SEGMENTS) {
		const node = humanoid.getRawBoneNode?.(def.bone) || humanoid.getNormalizedBoneNode?.(def.bone);
		if (!node) return null;
		bones[def.bone] = node;
	}
	const root = vrm.scene;
	if (!root) return null;
	root.updateWorldMatrix(true, true);
	const box = new THREE.Box3().setFromObject(root);
	const size = new THREE.Vector3();
	box.getSize(size);
	return {
		bones,
		height: size.y > .2 && isFinite(size.y) ? size.y : 1.7,
		groundY: box.min.y
	};
}
const _v1 = new THREE.Vector3();
const _v2 = new THREE.Vector3();
const _v3 = new THREE.Vector3();
const _q1$1 = new THREE.Quaternion();
const _q2$1 = new THREE.Quaternion();
const _q3$1 = new THREE.Quaternion();
const _m1 = new THREE.Matrix4();
var ActiveRagdoll = class {
	RAPIER;
	rig;
	tuning;
	world;
	parts = [];
	jointHandles = [];
	totalMass = 0;
	groundY;
	scale;
	/** 局部地面高度查询钩子：训练时指向体素地形，浏览器不设 → 恒为 groundY */
	groundProbe = null;
	/** 地形高度查询钩子（真按 x/z 查）：与 groundProbe 分开 —— 后者是「支撑面」，忽略坐标 */
	terrainProbe = null;
	downed = false;
	downTimer = 0;
	recoverT = 0;
	stepMs = 0;
	lastSpeed = 0;
	/** 策略残差：每段 3 维旋转向量（世界系，弧度），叠加到驱动目标上。null = 纯 PD 控制 */
	policyResidual = null;
	constructor(RAPIER, rig, tuning) {
		this.RAPIER = RAPIER;
		this.rig = rig;
		this.tuning = {
			...DEFAULT_TUNING,
			...tuning || {}
		};
		this.scale = scaleFor(rig.height);
		this.groundY = rig.groundY;
		this.world = new RAPIER.World({
			x: 0,
			y: -9.81 * this.tuning.gravityScale,
			z: 0
		});
		this.world.numSolverIterations = 8;
		this.build();
	}
	build() {
		const RAPIER = this.RAPIER;
		const scale = this.scale;
		if (this.tuning.groundEnabled) {
			const groundBody = this.world.createRigidBody(RAPIER.RigidBodyDesc.fixed().setTranslation(0, this.groundY - .5, 0));
			this.world.createCollider(RAPIER.ColliderDesc.cuboid(30, .5, 30).setFriction(this.tuning.groundFriction), groundBody);
		}
		for (const def of SEGMENTS) {
			const bone = this.rig.bones[def.bone];
			const p = bone.getWorldPosition(new THREE.Vector3());
			const q = bone.getWorldQuaternion(new THREE.Quaternion());
			const mass = def.mass * scale * scale * scale;
			this.totalMass += mass;
			const body = this.world.createRigidBody(RAPIER.RigidBodyDesc.dynamic().setTranslation(p.x, p.y, p.z).setRotation({
				x: q.x,
				y: q.y,
				z: q.z,
				w: q.w
			}).setLinearDamping(.05).setAngularDamping(.35).setCanSleep(false).setCcdEnabled(true));
			const child = def.child ? this.rig.bones[def.child] : null;
			let len = def.fixedLen ? def.fixedLen * scale : 0;
			if (child) len = child.getWorldPosition(_v1).distanceTo(p);
			if (!(len > .01)) len = .1 * scale;
			const radius = Math.max(.02, def.radius * scale);
			let colliderDesc;
			if (def.ballOffsetY !== void 0) colliderDesc = RAPIER.ColliderDesc.ball(radius).setTranslation(0, def.ballOffsetY * scale, 0);
			else {
				const half = Math.max(.01, len / 2 - radius);
				const dir = _v3.set(0, 1, 0);
				if (child) {
					dir.copy(child.getWorldPosition(_v1)).sub(p);
					if (dir.lengthSq() > 1e-9) dir.normalize().applyQuaternion(_q1$1.copy(q).invert());
					else dir.set(0, 1, 0);
				}
				_q2$1.setFromUnitVectors(_v1.set(0, 1, 0), dir);
				colliderDesc = RAPIER.ColliderDesc.capsule(half, radius).setTranslation(dir.x * len / 2, dir.y * len / 2, dir.z * len / 2).setRotation({
					x: _q2$1.x,
					y: _q2$1.y,
					z: _q2$1.z,
					w: _q2$1.w
				});
			}
			colliderDesc.setMass(mass).setFriction(.9).setRestitution(.02);
			this.world.createCollider(colliderDesc, body);
			let sole;
			if (def.bone.endsWith("Foot")) {
				const drop = p.y - this.groundY - radius;
				if (drop > 0) {
					_v2.set(0, -drop, 0).applyQuaternion(_q1$1.copy(q).invert());
					this.world.createCollider(RAPIER.ColliderDesc.ball(radius).setTranslation(_v2.x, _v2.y, _v2.z).setMass(mass * .4).setFriction(.9).setRestitution(.02), body);
					sole = {
						x: _v2.x,
						y: _v2.y,
						z: _v2.z,
						r: radius,
						bindGap: p.y - this.groundY
					};
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
				maxDeviationCos: Math.cos(def.maxDeviation * Math.PI / 180),
				hasParent: parentIndex >= 0,
				sole
			});
		}
		for (let i = 0; i < this.parts.length; i++) {
			const part = this.parts[i];
			if (!part.hasParent) continue;
			const parent = this.parts[part.parentIndex];
			const jointPoint = part.bone.getWorldPosition(new THREE.Vector3());
			const pt = parent.body.translation();
			const pr = parent.body.rotation();
			_q1$1.set(pr.x, pr.y, pr.z, pr.w).invert();
			_v1.set(jointPoint.x - pt.x, jointPoint.y - pt.y, jointPoint.z - pt.z).applyQuaternion(_q1$1);
			const data = RAPIER.JointData.spherical({
				x: _v1.x,
				y: _v1.y,
				z: _v1.z
			}, {
				x: 0,
				y: 0,
				z: 0
			});
			this.jointHandles.push(this.world.createImpulseJoint(data, parent.body, part.body, true));
			const parentQ = parent.bone.getWorldQuaternion(new THREE.Quaternion());
			const selfQ = part.bone.getWorldQuaternion(new THREE.Quaternion());
			part.restRelQ.copy(parentQ).invert().multiply(selfQ);
		}
	}
	update(dt) {
		if (!this.parts.length) return;
		const t0 = typeof performance !== "undefined" ? performance.now() : Date.now();
		this.readTargets();
		this.updateDowned(dt);
		this.applyPolicyResidual();
		if (this.isOutOfWorld()) this.resetToTargets();
		const subSteps = Math.max(1, Math.min(4, this.tuning.subSteps | 0));
		const sub = dt / subSteps;
		this.world.timestep = sub;
		for (let s = 0; s < subSteps; s++) {
			this.applyDrive(sub);
			this.world.step();
		}
		this.writeBack();
		const v = this.parts[0].body.linvel();
		this.lastSpeed = Math.hypot(v.x, v.y, v.z);
		this.stepMs = (typeof performance !== "undefined" ? performance.now() : Date.now()) - t0;
	}
	/** 从动画姿态读目标；倒下期间冻结驱动目标，起身时插值回动画目标 */
	readTargets() {
		const ramp = this.tuning.recoverRamp > 0 ? 1 / this.tuning.recoverRamp : 1;
		for (const part of this.parts) {
			part.targetQ.copy(part.bone.getWorldQuaternion(_q1$1));
			if (part.parentIndex < 0) part.targetP.copy(part.bone.getWorldPosition(_v1));
			if (!this.downed) {
				part.driveQ.copy(part.targetQ);
				if (part.parentIndex < 0) part.driveP.copy(part.targetP);
				this.recoverT = 0;
			} else if (this.recoverT > 0) {
				part.driveQ.slerp(part.targetQ, ramp);
				if (part.parentIndex < 0) part.driveP.lerp(part.targetP, ramp);
			}
		}
	}
	/** 倒下判定与起身节奏：髋高掉到站立髋高的 62% 以下算倒 */
	updateDowned(dt) {
		const hip = this.parts[0];
		const hipY = hip.body.translation().y;
		const standHipY = hip.targetP.y;
		const fallY = this.groundY + (standHipY - this.groundY) * this.tuning.downedHeightRatio;
		const low = hipY < fallY && this.lastSpeed < 1.5;
		if (!this.downed) {
			if (low) {
				this.downTimer += dt;
				if (this.downTimer > .5) {
					this.downed = true;
					this.downTimer = 0;
					this.recoverT = 0;
				}
			} else this.downTimer = 0;
			return;
		}
		if (this.recoverT === 0) {
			this.downTimer += dt;
			if (this.downTimer > this.tuning.recoverDelay) {
				this.downTimer = 0;
				this.recoverT = 1e-6;
			}
			return;
		}
		this.recoverT += dt;
		if (this.recoverT > this.tuning.recoverRamp && hipY > fallY + .05) {
			this.downed = false;
			this.recoverT = 0;
			this.downTimer = 0;
		}
	}
	/** PD 驱动：角向扭矩拉姿态 + 髋部线性弹簧托住身体（平衡辅助） */
	applyDrive(dt) {
		const recoverFactor = this.downed ? Math.min(1, this.recoverT / Math.max(.001, this.tuning.recoverRamp)) : 1;
		const balance = this.tuning.balance * recoverFactor;
		for (const part of this.parts) {
			const body = part.body;
			const rot = body.rotation();
			_q1$1.set(rot.x, rot.y, rot.z, rot.w);
			_q2$1.copy(part.driveQ).multiply(_q3$1.copy(_q1$1).invert());
			if (_q2$1.w < 0) _q2$1.set(-_q2$1.x, -_q2$1.y, -_q2$1.z, -_q2$1.w);
			const w = Math.min(1, Math.max(-1, _q2$1.w));
			const angle = 2 * Math.acos(w);
			const sinHalf = Math.sqrt(Math.max(0, 1 - w * w));
			let ex = 0, ey = 0, ez = 0;
			if (sinHalf > 1e-5 && angle > 1e-5) {
				const k = angle / sinHalf;
				ex = _q2$1.x * k;
				ey = _q2$1.y * k;
				ez = _q2$1.z * k;
			}
			const omega = body.angvel();
			let tx = part.kp * ex - part.kd * omega.x;
			let ty = part.kp * ey - part.kd * omega.y;
			let tz = part.kp * ez - part.kd * omega.z;
			if (part.hasParent) {
				const pr = this.parts[part.parentIndex].body.rotation();
				_q2$1.set(pr.x, pr.y, pr.z, pr.w);
				_q3$1.copy(_q2$1).invert().multiply(_q1$1);
				_q1$1.copy(part.restRelQ).multiply(_q3$1.invert());
				if (_q1$1.w < 0) _q1$1.set(-_q1$1.x, -_q1$1.y, -_q1$1.z, -_q1$1.w);
				const lw = Math.min(1, _q1$1.w);
				const devAngle = 2 * Math.acos(lw);
				const devLimit = Math.acos(Math.min(1, Math.max(-1, part.maxDeviationCos)));
				const s = Math.sqrt(Math.max(0, 1 - lw * lw));
				if (devAngle > devLimit && s > 1e-5) {
					const over = devAngle - devLimit;
					_v1.set(_q1$1.x / s, _q1$1.y / s, _q1$1.z / s).applyQuaternion(_q2$1);
					const k = over * part.kp * .5;
					tx += _v1.x * k;
					ty += _v1.y * k;
					tz += _v1.z * k;
				}
			}
			const maxT = part.maxTorque;
			const tLen = Math.hypot(tx, ty, tz);
			if (tLen > maxT) {
				const k = maxT / tLen;
				tx *= k;
				ty *= k;
				tz *= k;
			}
			body.applyTorqueImpulse({
				x: tx * dt,
				y: ty * dt,
				z: tz * dt
			}, true);
			if (!part.hasParent && balance > 0) {
				const t = body.translation();
				const v = body.linvel();
				const m = body.mass();
				const ax = this.tuning.hipKp * (part.driveP.x - t.x) - this.tuning.hipKd * v.x;
				const ay = this.tuning.hipKp * (part.driveP.y - t.y) - this.tuning.hipKd * v.y;
				const az = this.tuning.hipKp * (part.driveP.z - t.z) - this.tuning.hipKd * v.z;
				body.applyImpulse({
					x: m * ax * balance * dt,
					y: m * ay * balance * dt,
					z: m * az * balance * dt
				}, true);
			}
		}
	}
	/** 物理位姿写回骨骼：髋写位置+旋转，其余只写旋转（保持骨架长度，不拉长肢体） */
	writeBack() {
		for (const part of this.parts) {
			const t = part.body.translation();
			const r = part.body.rotation();
			_q1$1.set(r.x, r.y, r.z, r.w);
			const parent = part.bone.parent;
			if (parent) {
				parent.getWorldQuaternion(_q2$1).invert();
				_q3$1.copy(_q2$1).multiply(_q1$1);
				part.bone.quaternion.copy(_q3$1);
			} else part.bone.quaternion.copy(_q1$1);
			if (!part.hasParent) {
				if (parent) {
					_v2.set(t.x, t.y, t.z).applyMatrix4(_m1.copy(parent.matrixWorld).invert());
					part.bone.position.copy(_v2);
				} else part.bone.position.set(t.x, t.y, t.z);
			}
		}
	}
	/** 掉出世界兜底：被推飞或数值发散时把身体搬回动画姿态，别让角色消失在虚空里。
	*  水平判据用「距动画髋目标的距离」而不是绝对坐标 —— 角色被摆在场景任何位置都不误判。 */
	isOutOfWorld() {
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
	resetToTargets() {
		for (const part of this.parts) {
			const p = part.bone.getWorldPosition(_v1);
			const q = part.bone.getWorldQuaternion(_q1$1);
			part.body.setTranslation({
				x: p.x,
				y: p.y,
				z: p.z
			}, true);
			part.body.setRotation({
				x: q.x,
				y: q.y,
				z: q.z,
				w: q.w
			}, true);
			part.body.setLinvel({
				x: 0,
				y: 0,
				z: 0
			}, true);
			part.body.setAngvel({
				x: 0,
				y: 0,
				z: 0
			}, true);
			part.driveQ.copy(part.targetQ);
			part.driveP.copy(part.targetP);
		}
		this.downed = false;
		this.downTimer = 0;
		this.recoverT = 0;
		this.lastSpeed = 0;
	}
	/** 把策略残差叠加到驱动目标上：世界系左乘，零动作 = 原 PD 行为 */
	applyPolicyResidual() {
		const res = this.policyResidual;
		if (!res) return;
		const n = Math.min(this.parts.length, res.length / 3 | 0);
		for (let i = 0; i < n; i++) {
			const o = i * 3;
			const rx = res[o], ry = res[o + 1], rz = res[o + 2];
			if (rx === 0 && ry === 0 && rz === 0) continue;
			const ang = Math.sqrt(rx * rx + ry * ry + rz * rz);
			const s = ang > 1e-6 ? Math.sin(ang / 2) / ang : .5;
			_q1$1.set(rx * s, ry * s, rz * s, Math.cos(ang / 2));
			this.parts[i].driveQ.premultiply(_q1$1);
		}
	}
	/** 只读访问：策略观测/训练环境需要遍历物理段 */
	getParts() {
		return this.parts;
	}
	getRapier() {
		return this.RAPIER;
	}
	getWorld() {
		return this.world;
	}
	getGroundY() {
		return this.groundY;
	}
	/**
	* 设局部地面高度查询（起伏地形用）。观测里的「髋高比」是相对脚下地面的
	* 量，不是相对某块基准平板 —— 站在 40cm 高的台阶上时，前者才是角色能
	* 感知到的本体信息，后者会把台阶高度误当成「自己长高了」。
	*/
	setGroundProbe(fn) {
		this.groundProbe = fn;
	}
	/** 点 (x,z) 处的地面高度：有 probe 就问它，否则就是内置平板高度 */
	groundHeightAt(x, z) {
		return this.groundProbe ? this.groundProbe(x, z) : this.groundY;
	}
	/**
	* 设地形高度查询（按坐标，观测探针用）。与 setGroundProbe 的区别：那个返回
	* 「支撑面高度」（髋+双脚三点取最高，与坐标无关），是奖励和髋高比的基准；
	* 观测要的是「身前那格有多高」，必须真按坐标查，否则探针恒等于支撑面。
	*/
	setTerrainProbe(fn) {
		this.terrainProbe = fn;
	}
	/** 点 (x,z) 处的地形高度：无 probe（平地）时就是基准地面 */
	terrainHeightAt(x, z) {
		return this.terrainProbe ? this.terrainProbe(x, z) : this.groundY;
	}
	getScale() {
		return this.scale;
	}
	getTotalMass() {
		return this.totalMass;
	}
	getDowned() {
		return this.downed;
	}
	/** 推一把：水平冲量，强度 1 ≈ 让 70kg 的人踉跄一步 */
	push(strength = 1, dirX = 0, dirZ = -1) {
		const len = Math.hypot(dirX, dirZ) || 1;
		const impulse = 260 * strength * this.totalMass / 71;
		for (const part of this.parts) {
			const w = part.parentIndex < 0 ? .5 : part.def.bone === "chest" ? .5 : 0;
			if (w <= 0) continue;
			part.body.applyImpulse({
				x: dirX / len * impulse * w,
				y: 0,
				z: dirZ / len * impulse * w
			}, true);
		}
	}
	setTuning(patch) {
		Object.assign(this.tuning, patch);
		const scale = this.scale;
		this.world.gravity = {
			x: 0,
			y: -9.81 * this.tuning.gravityScale,
			z: 0
		};
		for (const part of this.parts) {
			part.kp = part.def.kp * this.tuning.follow;
			part.maxTorque = part.def.mass * scale * scale * scale * 9.81 * this.tuning.maxTorquePerKg;
		}
	}
	getTuning() {
		return { ...this.tuning };
	}
	/** 屏上诊断快照：手机端没有控制台，这些量只有画到页面上才拿得到证据 */
	diag() {
		const feet = [];
		for (const p of this.parts) {
			if (!p.sole) continue;
			const t = p.body.translation();
			const r = p.body.rotation();
			_q1$1.set(r.x, r.y, r.z, r.w);
			_v1.set(p.sole.x, p.sole.y, p.sole.z).applyQuaternion(_q1$1);
			feet.push({
				bone: p.def.bone,
				soleGap: t.y + _v1.y - p.sole.r - this.groundY,
				bindGap: p.sole.bindGap
			});
		}
		const hip = this.parts[0];
		return {
			groundY: this.groundY,
			scale: this.scale,
			height: this.rig.height,
			hipY: hip ? hip.body.translation().y : 0,
			standHipY: hip ? hip.targetP.y : 0,
			feet
		};
	}
	getState() {
		const hip = this.parts[0];
		return {
			enabled: true,
			bodies: this.parts.length,
			hipHeight: hip ? hip.body.translation().y - this.groundY : 0,
			speed: this.lastSpeed,
			downed: this.downed,
			stepMs: this.stepMs
		};
	}
	/** 释放物理世界（切换模型 / 关掉物理时调用） */
	dispose() {
		for (const j of this.jointHandles) this.world.removeImpulseJoint(j, false);
		this.jointHandles.length = 0;
		this.parts.length = 0;
		this.world.free();
	}
};
//#endregion
//#region web/js/physics/policy-io.ts
const PART_COUNT = SEGMENTS.length;
const OBS_PER_PART = 6;
const ROOT_OBS = 9;
/** 地形探针：8 个方向 × 2 个半径，相对脚下地面的高差 —— 补上「盲走」缺的那只眼 */
const PROBE_DIRS = 8;
const PROBE_RADII = [.4, .8];
const TERRAIN_OBS = 8 * PROBE_RADII.length;
/** 探针高差归一化：实测最大高差 0.72m（坑底望高台），除 0.4 后落在 ±1.8，
*  与观测里其他项的 O(1) 量级对齐；单层台阶（8cm）读数 0.2，仍能分辨 */
const PROBE_SCALE = 1 / .4;
/** 不含上一帧动作的观测维度 */
const OBS_DIM = PART_COUNT * 6 + 9 + TERRAIN_OBS;
const ACT_DIM = PART_COUNT * 3;
/** 网络实际输入维度（含上一帧动作） */
const OBS_DIM_FULL = OBS_DIM + ACT_DIM;
const ANG_SCALE = 1;
const ANGVEL_SCALE = 1 / 8;
const VEL_SCALE = 1 / 3;
const POS_SCALE = 1 / .4;
/** 单段旋转残差上限（弧度）—— 太大会让 PD 目标飞掉，物理发散 */
const ACTION_LIMIT = .6;
const _q1 = new THREE.Quaternion();
const _q2 = new THREE.Quaternion();
const _q3 = new THREE.Quaternion();
new THREE.Vector3();
/** 旋转向量（轴角，世界系）写入 out[k..k+2]；用 sinc 形式避免除零 */
function writeRotvec(q, out, k, scale) {
	if (q.w < 0) _q3.set(-q.x, -q.y, -q.z, -q.w);
	else _q3.copy(q);
	const w = Math.min(1, Math.max(-1, _q3.w));
	const ang = 2 * Math.acos(w);
	const s = Math.sqrt(Math.max(0, 1 - w * w));
	const f = s > 1e-5 && ang > 1e-5 ? ang / s * scale : 0;
	out[k] = _q3.x * f;
	out[k + 1] = _q3.y * f;
	out[k + 2] = _q3.z * f;
}
/**
* 地形探针：以脚下地形为基准，沿躯干朝向均布 8 个方向、2 个半径各采一次高差。
* 没有地形钩子时（浏览器平地）全部读出 0 —— 那是「脚下是平的」的正确读数，
* 不是缺失值。
*/
function writeTerrainProbe(rd, out, k, x, z, q) {
	let fx = 2 * (q.x * q.z + q.w * q.y);
	let fz = 1 - 2 * (q.x * q.x + q.y * q.y);
	const flen = Math.sqrt(fx * fx + fz * fz);
	if (flen > .001) {
		fx /= flen;
		fz /= flen;
	} else {
		fx = 1;
		fz = 0;
	}
	const base = rd.terrainHeightAt(x, z);
	for (let ri = 0; ri < PROBE_RADII.length; ri++) {
		const rad = PROBE_RADII[ri];
		for (let d = 0; d < 8; d++) {
			const a = d / 8 * Math.PI * 2;
			const ca = Math.cos(a), sa = Math.sin(a);
			const dx = fx * ca - fz * sa;
			const dz = fz * ca + fx * sa;
			out[k++] = (rd.terrainHeightAt(x + dx * rad, z + dz * rad) - base) * PROBE_SCALE;
		}
	}
}
/**
* 组装观测（不含上一帧动作），返回写入结束位置。
* @param standHipY 站立时髋部高度（世界单位）—— 归一化基准
* @param phase 步态相位（弧度），timeRatio 0~1 的 episode 进度
*/
function buildObservation(rd, out, offset, standHipY, phase, timeRatio) {
	const parts = rd.getParts();
	let k = offset;
	for (let i = 0; i < parts.length; i++) {
		const p = parts[i];
		const r = p.body.rotation();
		_q1.set(r.x, r.y, r.z, r.w);
		_q2.copy(p.driveQ).multiply(_q3.copy(_q1).invert());
		writeRotvec(_q2, out, k, 1);
		k += 3;
		const w = p.body.angvel();
		out[k++] = w.x * ANGVEL_SCALE;
		out[k++] = w.y * ANGVEL_SCALE;
		out[k++] = w.z * ANGVEL_SCALE;
	}
	const hip = parts[0];
	const t = hip.body.translation();
	const v = hip.body.linvel();
	out[k++] = (t.y - rd.groundHeightAt(t.x, t.z)) / Math.max(.001, standHipY) - 1;
	out[k++] = v.x * VEL_SCALE;
	out[k++] = v.y * VEL_SCALE;
	out[k++] = v.z * VEL_SCALE;
	out[k++] = (t.x - hip.driveP.x) * POS_SCALE;
	out[k++] = (t.z - hip.driveP.z) * POS_SCALE;
	out[k++] = Math.sin(phase);
	out[k++] = Math.cos(phase);
	out[k++] = timeRatio;
	writeTerrainProbe(rd, out, k, t.x, t.z, hip.body.rotation());
	k += TERRAIN_OBS;
	return k;
}
/** 把动作写进物理侧残差缓冲（自动限幅、复用缓冲避免每帧分配） */
function writeAction(rd, act, offset = 0, count = ACT_DIM) {
	let res = rd.policyResidual;
	if (!res || res.length !== count) {
		res = new Float32Array(count);
		rd.policyResidual = res;
	}
	for (let i = 0; i < count; i++) {
		const v = act[offset + i];
		res[i] = v > .6 ? ACTION_LIMIT : v < -.6 ? -.6 : v;
	}
}
/** 观测 + 上一帧动作拼成网络输入（prevAct 为 null 时补零） */
function buildNetInput(rd, out, prevAct, standHipY, phase, timeRatio) {
	const k = buildObservation(rd, out, 0, standHipY, phase, timeRatio);
	for (let i = 0; i < ACT_DIM; i++) out[k + i] = prevAct ? prevAct[i] : 0;
}
/** 站立髋高：从当前骨骼动画姿态的髋位置估（reset 后、物理还没跑时调） */
function measureStandHipY(rd) {
	const hip = rd.getParts()[0];
	return Math.max(.001, hip.driveP.y - rd.groundHeightAt(hip.driveP.x, hip.driveP.z));
}
/** 躯干倾角（弧度）：髋段世界朝向与驱动目标朝向的夹角 */
function torsoTilt(rd) {
	const hip = rd.getParts()[0];
	const r = hip.body.rotation();
	_q1.set(r.x, r.y, r.z, r.w);
	_q2.copy(hip.driveQ).multiply(_q3.copy(_q1).invert());
	const w = Math.min(1, Math.abs(_q2.w));
	return 2 * Math.acos(w);
}
//#endregion
export { ACTION_LIMIT, ACT_DIM, ANGVEL_SCALE, ANG_SCALE, ActiveRagdoll, DEFAULT_TUNING, HEIGHT_REF, OBS_DIM, OBS_DIM_FULL, OBS_PER_PART, PART_COUNT, POS_SCALE, PROBE_DIRS, PROBE_RADII, PROBE_SCALE, ROOT_OBS, SEGMENTS, TERRAIN_OBS, VEL_SCALE, buildNetInput, buildObservation, collectRig, loadRapier, measureStandHipY, scaleFor, torsoTilt, writeAction };
