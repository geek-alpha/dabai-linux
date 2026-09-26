/* ============================================================
 * 43 主动布娃娃模式 —— 开关 / 每帧步进 / 交互
 * ------------------------------------------------------------
 * 接线点：包一层 App.animateModel。
 * animateModel 内部按动画姿态写骨骼、末尾调 vrm.update(dt)（humanoid +
 * spring bone），所以它的返回点正好是「本帧动画姿态已就位、还没渲染」——
 * 物理在这一刻读目标、步进、把物理位姿写回 raw 骨骼，本帧画出来的就是
 * 物理结果，不差一帧。下一帧动画系统照常覆盖骨骼，物理目标随之刷新，
 * 所以不会累积误差。
 *
 * 开关：本机访问默认开（?ragdoll=ml 神经网络 / ?ragdoll=1 纯 PD /
 * ?ragdoll=0 强制关）；Shift+R 切换；P 推一把（开启时）。
 * 降级：物理耗时 EMA 超阈值先降子步，再超就自动关模式（低配机型保帧率）。
 * ============================================================ */

import type { AppKernel } from '../types/app-kernel.js';
import { ActiveRagdoll, collectRig, loadRapier } from './active-ragdoll.js';
import { DEFAULT_TUNING } from './ragdoll-config.js';
import type { RagdollState } from './active-ragdoll.js';
import type { RagdollTuning } from './ragdoll-config.js';
import { PolicyNet } from './policy-net.js';
import { RagdollPolicy } from './ragdoll-policy.js';
import { measureStandHipY } from './policy-io.js';

/** 训练产物位置：tools/train/train.mjs 写、server.py 直接服务 web/ 目录 */
const POLICY_URL = '/generated/policy/ragdoll-v3.json';

export default (function init_43_ragdoll(App: AppKernel) {
  App.ragdollMode = false;
  App.ragdoll = null;
  App.ragdollStatus = 'off';
  App._ragdollVrm = null;
  App._ragdollBusy = false;
  App._ragdollEma = 0;
  App._ragdollTuning = { ...DEFAULT_TUNING };
  App.ragdollPolicyMode = 'pd';
  App.ragdollPolicy = null;
  App._ragdollPolicyNet = null;
  App._ragdollSlowNoticed = false;
  App._ragdollSlowFrames = 0;

  const EMPTY_STATE: RagdollState = {
    enabled: false, bodies: 0, hipHeight: 0, speed: 0, downed: false, stepMs: 0,
  };

  function teardown() {
    if (App.ragdoll) {
      try { App.ragdoll.dispose(); } catch (e) { console.warn('[ragdoll] dispose 失败', e); }
      App.ragdoll = null;
    }
    App.ragdollPolicy = null;
    App._ragdollVrm = null;
    App._ragdollEma = 0;
  }

  /* ---------------- 屏上诊断 HUD ----------------
   * 手机端没有控制台：物理状态只有画在页面上才拿得到证据（一张截图就够定位）。
   * ?ragdoll=1 自动带出，?hud=0 关掉。 */
  let hudEl: HTMLDivElement | null = null;
  function hudTick() {
    if (!hudEl) return;
    const st = App.ragdollState();
    const d = App.ragdoll?.diag();
    const feet = d && d.feet.length
      ? d.feet.map((f) => `${f.bone === 'leftFoot' ? 'L' : 'R'}球底${(f.soleGap * 100).toFixed(1)} 绑踝${(f.bindGap * 100).toFixed(1)}`).join(' | ')
      : '无脚球（脚段未命中或 drop<=0）';
    let cm = '?';
    try { cm = JSON.parse(localStorage.getItem('dabai.currentModel') || '{}').name || '?'; } catch { /* 无 localStorage 时忽略 */ }
    hudEl.textContent =
      `model=${cm} ragdoll=${App.ragdollStatus} policy=${App.ragdollPolicyMode} bodies=${st.bodies} downed=${st.downed ? 'Y' : 'N'}\n` +
      (d
        ? `groundY=${d.groundY.toFixed(3)} 身高=${d.height.toFixed(2)} 缩放=${d.scale.toFixed(3)}\n` +
          `${feet}（cm）\n` +
          `髋 ${d.hipY.toFixed(3)} / 站 ${d.standHipY.toFixed(3)} = ${d.standHipY ? (d.hipY / d.standHipY).toFixed(2) : '-'}\n`
        : '物理身体未建立\n') +
      `step=${st.stepMs.toFixed(1)}ms hipH=${st.hipHeight.toFixed(2)}m`;
  }
  function hudShow() {
    if (hudEl) return;
    hudEl = document.createElement('div');
    hudEl.id = 'ragdoll-hud';
    hudEl.style.cssText =
      'position:fixed;left:8px;top:64px;z-index:99999;padding:6px 8px;' +
      'font:11px/1.5 ui-monospace,monospace;color:#9ef;background:rgba(0,0,0,.66);' +
      'border:1px solid rgba(120,220,255,.35);border-radius:6px;white-space:pre;pointer-events:none';
    document.body.appendChild(hudEl);
    window.setInterval(hudTick, 250);
    hudTick();
  }

  /** 挂上策略：加载权重 → 建控制器 → 关掉手工髋部支撑（平衡交给网络） */
  async function attachPolicy() {
    if (!App.ragdoll) return false;
    if (!App._ragdollPolicyNet) {
      try {
        App._ragdollPolicyNet = await PolicyNet.load(POLICY_URL);
      } catch (e) {
        console.warn('[ragdoll] 策略权重加载失败，退回纯 PD', e);
        App.ragdollPolicyMode = 'pd';
        return false;
      }
    }
    const pol = new RagdollPolicy(App._ragdollPolicyNet);
    pol.reset(measureStandHipY(App.ragdoll));
    App.ragdollPolicy = pol;
    applyPolicyTuning(true);
    console.info(
      `[ragdoll] 策略接管：观测 ${App._ragdollPolicyNet.obsDim} 维 → 动作 ${App._ragdollPolicyNet.actDim} 维，` +
      `训练步数 ${(App._ragdollPolicyNet.meta as any)?.steps ?? '?'}`
    );
    return true;
  }

  function applyTuning(patch: Partial<RagdollTuning>) {
    Object.assign(App._ragdollTuning, patch);
    if (App.ragdoll) App.ragdoll.setTuning(patch);
  }

  /** 策略接管 = 把物理口径切回训练分布；detach 时恢复用户口径。
   *  训练时 maxTorquePerKg 1.0~3.0、balance 0~0.35、subSteps 1、
   *  downedHeightRatio 0，而前端默认是「40 N·m/kg 锁成雕像」的 PD 档 —— 同一份
   *  权重放进后者的动力学里，动作被放大 20 倍执行，实测就是满地踉跄乱走。
   *  实测（tools/train/_balance_ab.mjs）：balance<0.1 的局髋高比 0.45（站不住），
   *  >=0.1 的局 0.81 —— 策略还没学会撤掉手工辅助站立，先给分布内的 0.25。 */
  function applyPolicyTuning(on: boolean) {
    if (!App.ragdoll) return;
    const t = App._ragdollTuning;
    App.ragdoll.setTuning(on
      ? { balance: 0.25, maxTorquePerKg: 2.0, subSteps: 1, downedHeightRatio: 0 }
      : {
        balance: t.balance, maxTorquePerKg: t.maxTorquePerKg,
        subSteps: t.subSteps, downedHeightRatio: t.downedHeightRatio,
      });
  }

  /** 建物理身体（rapier wasm 首次约 200~400ms，异步加载期间模式可能被关掉） */
  async function build(vrm: any) {
    if (App._ragdollBusy) return;
    App._ragdollBusy = true;
    App.ragdollStatus = 'loading';
    try {
      const RAPIER = await loadRapier();
      if (!App.ragdollMode || App.vrm !== vrm) {
        App.ragdollStatus = App.ragdollMode ? 'loading' : 'off';
        return;
      }
      const rig = collectRig(vrm);
      if (!rig) {
        App.ragdollStatus = 'error';
        console.warn('[ragdoll] 骨骼不全（VRM 缺 humanoid 关键骨骼），无法建物理身体');
        return;
      }
      teardown();
      App.ragdoll = new ActiveRagdoll(RAPIER, rig, App._ragdollTuning);
      App._ragdollVrm = vrm;
      App.ragdollStatus = 'on';
      console.info(`[ragdoll] 物理身体就绪：${App.ragdollState().bodies} 段，身高 ${rig.height.toFixed(2)}m，地面 y=${rig.groundY.toFixed(2)}`);
      if (App.ragdollPolicyMode === 'ml') await attachPolicy();
    } catch (e) {
      App.ragdollStatus = 'error';
      console.warn('[ragdoll] rapier 加载失败（离线/构建未打包 wasm？）', e);
    } finally {
      App._ragdollBusy = false;
    }
  }

  App.enableRagdoll = async function enableRagdoll(on: boolean) {
    App.ragdollMode = on !== false;
    if (!App.ragdollMode) {
      teardown();
      App.ragdollStatus = 'off';
      return false;
    }
    // 模型还没就位：先挂上模式，帧钩子等 vrm 出现再建
    if (App.modelType !== 'vrm' || !App.vrm) {
      App.ragdollStatus = 'loading';
      return false;
    }
    await build(App.vrm);
    return App.ragdollStatus === 'on';
  };

  App.ragdollToggle = function ragdollToggle() {
    const next = !App.ragdollMode;
    void App.enableRagdoll(next);
    App.showToast?.(next ? '物理身体：开（Shift+R 关，P 推）' : '物理身体：关');
    return next;
  };

  App.setRagdollPolicy = async function setRagdollPolicy(mode: 'pd' | 'ml') {
    App.ragdollPolicyMode = mode;
    if (mode === 'ml') return attachPolicy();
    if (App.ragdollPolicy && App.ragdoll) App.ragdollPolicy.detach(App.ragdoll);
    App.ragdollPolicy = null;
    applyPolicyTuning(false);
    return true;
  };

  App.ragdollPush = function ragdollPush(strength = 1, dirX = 0, dirZ = -1) {
    if (!App.ragdoll) return false;
    App.ragdoll.push(strength, dirX, dirZ);
    return true;
  };

  App.ragdollState = function ragdollState() {
    return App.ragdoll ? App.ragdoll.getState() : { ...EMPTY_STATE };
  };

  App.setRagdollTuning = function setRagdollTuning(patch: Partial<RagdollTuning>) {
    applyTuning(patch);
  };

  App.getRagdollTuning = function getRagdollTuning() {
    return { ...App._ragdollTuning };
  };

  /* ---------------- 每帧 ---------------- */

  function ragdollFrame(dt: number) {
    if (!App.ragdollMode) return;

    const vrm = App.modelType === 'vrm' ? App.vrm : null;
    if (!vrm) {
      // 换成 gltf 模型 / 模型卸载：物理身体跟着撤
      if (App.ragdoll) teardown();
      return;
    }
    if (App.ragdoll && App._ragdollVrm !== vrm) teardown();
    if (!App.ragdoll) {
      if (!App._ragdollBusy) void build(vrm);
      return;
    }

    // 物理时间步不能由渲染帧率决定：掉到 2fps 时 dt=0.5s，Rapier 一步积分半秒
    // 直接炸（实测髋高飙到 3.8m、速度 17m/s、满大厅跑）。封顶 1/30 —— 慢机器上
    // 物理变成慢动作，但姿态是对的，比「看着像抽搐」强。
    const pdt = Math.min(dt, 1 / 30);
    if (App.ragdollPolicy) App.ragdollPolicy.update(App.ragdoll, pdt);
    App.ragdoll.update(pdt);

    // 降级：低功耗档直接单子步；再慢就关模式，别让物理吃掉帧率
    const st = App.ragdoll.getState();
    App._ragdollEma = App._ragdollEma * 0.9 + st.stepMs * 0.1;
    // 冷启动（wasm 实例化、策略权重解析、首次推理）单帧能到 200ms+，拿它
    // 判「帧率不足」会把刚开的模式当场关掉 —— 连续 30 帧慢才算真慢。
    App._ragdollSlowFrames = App._ragdollEma > 18 ? App._ragdollSlowFrames + 1 : 0;
    // 慢只提示、不改物理参数：subSteps 一变 timestep 就变，等于换了套物理，
    // 策略直接在训练分布外跑 —— 省下的那点帧率不值这个价。
    if (App._ragdollSlowFrames > 30 && !App._ragdollSlowNoticed) {
      App._ragdollSlowNoticed = true;
      App.showToast?.(`物理身体：步进 ${App._ragdollEma.toFixed(0)}ms 偏慢，继续跑（Shift+R 关）`);
    }
  }

  const origAnimateModel = App.animateModel;
  if (typeof origAnimateModel === 'function') {
    App.animateModel = function animateModelWithRagdoll(t: number, dt: number, mouthOpen: number) {
      origAnimateModel.call(App, t, dt, mouthOpen);
      try {
        ragdollFrame(dt);
      } catch (e) {
        console.warn('[ragdoll] 步进异常，已停用物理身体', e);
        void App.enableRagdoll(false);
      }
    };
  } else {
    console.warn('[ragdoll] 未找到 App.animateModel：本模块必须在 init_07_click_interact 之后加载');
  }

  /* ---------------- 交互 ---------------- */

  window.addEventListener('keydown', (e) => {
    const el = e.target as HTMLElement | null;
    const tag = el?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable) return;
    if (e.shiftKey && (e.key === 'R' || e.key === 'r')) {
      e.preventDefault();
      App.ragdollToggle();
    } else if (e.shiftKey && (e.key === 'M' || e.key === 'm')) {
      e.preventDefault();
      const next = App.ragdollPolicyMode === 'ml' ? 'pd' : 'ml';
      void App.setRagdollPolicy(next).then((ok) => {
        App.showToast?.(ok
          ? (next === 'ml' ? '控制器：神经网络策略（Shift+M 切回 PD）' : '控制器：纯 PD')
          : '策略权重加载失败，仍是纯 PD');
      });
    } else if ((e.key === 'p' || e.key === 'P') && App.ragdollMode) {
      e.preventDefault();
      App.ragdollPush(1, 0, -1);
    }
  });

  try {
    const q = new URLSearchParams(location.search).get('ragdoll');
    // 默认不抢全屏：boot 会把界面开成全屏聊天态，那是用户的默认视图，
    // 之前这里无条件 setChatFullscreen(false)，把默认全屏顶掉了。
    // 只有显式 ?ragdoll=1 才自动起物理并退出全屏 —— 全屏静默下 canvas 隐藏、
    // 帧循环停，物理一帧都不步进，两个动作必须同拍。
    // 平时按 Shift+R 开，同样会退出全屏（见下面键盘处理）。
    if (q === '1') {
      // 默认上策略：纯 PD 档是 40 N·m/kg 的「雕像」口径，实测脚被甩到离地 50cm，
      // 肉眼就是腿悬空（tools/train/_frontend_ab.mjs 对照：ML 档 7.8cm）。要纯 PD 传 policy=pd。
      App.ragdollPolicyMode = new URLSearchParams(location.search).get('policy') === 'pd' ? 'pd' : 'ml';
      // 手机上出证据：状态直接画在页面上（截图即可定位），hud=0 可关
      if (new URLSearchParams(location.search).get('hud') !== '0') hudShow();
      window.addEventListener('load', () => {
        setTimeout(() => {
          App.setChatFullscreen?.(false);
          void App.enableRagdoll(true);
        }, 300);
      });
    } else if (q !== '0' && q !== 'off') {
      // 不自动起，但把控制器定成策略：Shift+R 打开时直接用网络，不是纯 PD
      App.ragdollPolicyMode = 'ml';
    }
  } catch { /* 无 location（非浏览器环境）时忽略 */ }

  Object.defineProperty(window, '_ragdoll', {
    get: () => ({
      enable: App.enableRagdoll,
      toggle: App.ragdollToggle,
      push: App.ragdollPush,
      state: App.ragdollState,
      tuning: App.getRagdollTuning,
      setTuning: App.setRagdollTuning,
      setPolicy: App.setRagdollPolicy,
      get policyMode() { return App.ragdollPolicyMode; },
      get policy() { return App.ragdollPolicy; },
      get instance() { return App.ragdoll; },
    }),
  });
});
