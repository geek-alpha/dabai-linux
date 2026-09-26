/* ============================================================
 * web/js/vr/vr-voice.ts —— VR 语音控制带（模块 32 系列）
 * ------------------------------------------------------------
 * 缺口：手机 + VR 眼镜的沉浸会话里网页 DOM 不可见，而语音入口
 * #voice-btn（index.html）是 DOM 按钮 —— 戴上头显后用户没法说话。
 *
 * 形态：一条寄宿在「对话大屏」（vr-chat）底部的控制带。
 * 语音与对话本来就是一件事的两半：拆成两块浮空面板时用户要在两块
 * 之间来回瞄、还互相遮挡；合并成一块后上面读对话、下面说话，
 * 视线始终在一个方向。
 *
 * 职责边界（重要）：
 *   - 本模块只管「带」：画带、算带内命中、响应按钮。不建 mesh、
 *     不管摆位、不注册射线面板 —— 世界物体与命中入口只有宿主的
 *     一个，带内命中由宿主用 'v:' 前缀转回来（hitBand / trigger）。
 *   - 凝视点击（dwell）计时同样不在这里：vr-ray 统一仲裁。
 *
 * 按钮：主按钮（按住说话 / 聆听中 / 打断）、模式钮、取消钮（仅录音中）。
 * 电平条：auto 模式取 App.vadGetVolume() 真实频域能量；按住模式没有
 * analyser 时给计时脉冲 —— 不假装有电平，状态诚实。
 * 仅 WebXR 沉浸（非游戏模式）显示。
 * ============================================================ */
import type { AppKernel, VrVoice, VrVoiceButton } from '../types/app-kernel.js';

export default function initVrVoice(App: AppKernel) {
  if (App.vrVoice) return; // 幂等：避免重复初始化/重复包裹

  /* ---------------- 画布几何（像素；与宿主 vr-chat 画布同宽，1:1 贴上去） ---------------- */
  const CV_W = 1080;
  const CV_H = 140;                       // = bandH：宿主在画布底部预留这么多
  const PAD = 28;
  const CY = CV_H / 2;
  const MIC_R = 52;                       // 主按钮半径
  const MIC_X = PAD + MIC_R;
  const SIDE_R = 44;                      // 模式钮半径
  const SIDE_X = CV_W - PAD - SIDE_R;
  const CANCEL_R = 38;                    // 取消钮半径（仅录音中）
  const CANCEL_X = SIDE_X - SIDE_R - 16 - CANCEL_R;
  const TXT_X = MIC_X + MIC_R + 26;       // 中段左界（文字与电平条）
  const TXT_R = CANCEL_X - CANCEL_R - 22; // 中段右界：按「取消钮可能出现」留位，避免电平条宽度抖动
  const BAR_Y = 74;
  const BAR_H = 26;
  const GAZE_SNAP = 1.5;                  // 视线吸附半径（× 按钮半径）

  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';
  const C_ACCENT = '#7c5cff';
  const C_REC = '#f87171';

  const STATE_LABEL: Record<string, string> = {
    idle: '在线', thinking: '思考中', listening: '聆听中', speaking: '说话中'
  };

  /* ---------------- 带对象 ---------------- */
  const vv = App.vrVoice = {
    active: false,
    bandH: CV_H,
    cv: null,
    ctx: null,
    buttons: [],
    hoverId: '',
    focusId: '',
    flash: null,
    hold: false,
    recStart: 0,
    show: () => {},
    hide: () => {},
    markDirty: () => {},
    tick: () => false,
    drawBand: () => {},
    hitBand: () => null,
    pressDown: () => false,
    trigger: () => {}
  } as VrVoice;

  let dirty = true;

  /* ---------------- 绘制工具 ---------------- */
  function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function fitTo(ctx: CanvasRenderingContext2D, s: string, maxW: number, font: string): string {
    ctx.font = font;
    const t = String(s == null ? '' : s);
    if (ctx.measureText(t).width <= maxW) return t;
    let cut = t.length;
    while (cut > 1 && ctx.measureText(t.slice(0, cut) + '…').width > maxW) cut--;
    return t.slice(0, cut) + '…';
  }

  /** 当前是否在录音（含 VAD 的自动录音段） */
  function recording(): boolean {
    return !!App.isRecording || App.vadState === 'recording';
  }

  function ensureCanvas() {
    if (vv.cv) return;
    const cv = document.createElement('canvas');
    cv.width = CV_W;
    cv.height = CV_H;
    vv.cv = cv;
    vv.ctx = cv.getContext('2d');
    buildButtons();
    dirty = true;
  }

  /* ---------------- 布局：按钮表（随模式/录音状态变化） ---------------- */
  function buildButtons() {
    const auto = App.voiceMode === 'auto';
    const rec = recording();
    const btns: VrVoiceButton[] = [
      {
        id: 'mic',
        text: rec ? '发送' : (auto ? '聆听' : '按住'),
        x: MIC_X - MIC_R, y: CY - MIC_R, w: MIC_R * 2, h: MIC_R * 2
      },
      {
        id: 'mode',
        text: auto ? '按住' : '自动',
        x: SIDE_X - SIDE_R, y: CY - SIDE_R, w: SIDE_R * 2, h: SIDE_R * 2
      }
    ];
    // 取消只在录音中出现：常态放一颗「取消」在头显里是纯误触风险
    if (rec) {
      btns.push({
        id: 'cancel',
        text: '取消',
        x: CANCEL_X - CANCEL_R, y: CY - CANCEL_R, w: CANCEL_R * 2, h: CANCEL_R * 2
      });
    }
    vv.buttons = btns;
  }

  /* ---------------- 绘制 ---------------- */
  function drawCircleBtn(ctx: CanvasRenderingContext2D, b: VrVoiceButton, r: number, now: number,
    base: string, border: string, textCol: string) {
    const cx = b.x + b.w / 2;
    const cy = b.y + b.h / 2;
    // 手柄悬停与视线焦点同等对待：头显里两种设备都得看得见「现在指哪」
    const hovered = vv.hoverId === b.id || vv.focusId === b.id;
    const flashed = !!(vv.flash && vv.flash.id === b.id && now < vv.flash.until);
    const rr = r * (hovered ? 1.06 : 1);

    ctx.beginPath();
    ctx.arc(cx, cy, rr, 0, Math.PI * 2);
    ctx.fillStyle = flashed ? 'rgba(124,92,255,0.62)' : base;
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = (flashed || hovered) ? C_ACCENT : border;
    ctx.stroke();

    ctx.font = 'bold ' + Math.round(r * 0.5) + 'px ' + FONT;
    ctx.fillStyle = textCol;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(b.text, cx, cy + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  function draw() {
    const ctx = vv.ctx;
    if (!ctx) return;
    const now = performance.now();
    const auto = App.voiceMode === 'auto';
    const rec = recording();
    const st = String(App.currentState || 'idle');
    ctx.clearRect(0, 0, CV_W, CV_H);

    // 底板：与对话大屏同色系 —— 视觉上就是同一块面板的下半部分
    roundRect(ctx, 2, 2, CV_W - 4, CV_H - 4, 26);
    ctx.fillStyle = 'rgba(8,10,22,0.72)';
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(124,92,255,0.42)';
    ctx.stroke();

    // 第一行：模式 + 状态（录音中把计时也放这行）
    ctx.font = 'bold 26px ' + FONT;
    ctx.fillStyle = '#ffffff';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.fillText(auto ? '自动对话' : '按住说话', TXT_X, 40);

    const micState = rec ? (auto ? '聆听中' : '录音中') : '待命';
    let right = micState + ' · ' + (STATE_LABEL[st] || st);
    if (rec && vv.recStart) right = ((now - vv.recStart) / 1000).toFixed(1) + 's · ' + right;
    ctx.font = '24px ' + FONT;
    ctx.textAlign = 'right';
    ctx.fillStyle = rec ? C_REC : 'rgba(240,240,255,0.75)';
    ctx.fillText(right, TXT_R, 40);

    // 电平条：auto 用真实频域能量；录音中无 analyser 时给计时脉冲
    const barW = TXT_R - TXT_X;
    roundRect(ctx, TXT_X, BAR_Y, barW, BAR_H, BAR_H / 2);
    ctx.fillStyle = 'rgba(0,0,0,0.45)';
    ctx.fill();
    let level = 0;
    if (auto && App.vadAnalyser) level = Math.max(0, Math.min(1, App.vadGetVolume() * 2.2));
    else if (rec) level = 0.35 + 0.3 * Math.abs(Math.sin(now / 180));
    if (level > 0.001) {
      roundRect(ctx, TXT_X + 2, BAR_Y + 2, Math.max(BAR_H - 4, (barW - 4) * level), BAR_H - 4, (BAR_H - 4) / 2);
      ctx.fillStyle = auto ? 'rgba(56,189,248,0.85)' : 'rgba(248,113,113,0.85)';
      ctx.fill();
    }

    // 按钮
    for (const b of vv.buttons) {
      if (b.id === 'mic') {
        const speaking = st === 'speaking';
        const base = rec ? 'rgba(248,113,113,0.42)'
          : (auto && speaking) ? 'rgba(251,191,36,0.35)'
            : 'rgba(124,92,255,0.35)';
        const border = rec ? C_REC : (auto && speaking ? '#fbbf24' : 'rgba(124,92,255,0.7)');
        drawCircleBtn(ctx, b, MIC_R, now, base, border, '#ffffff');
      } else {
        drawCircleBtn(ctx, b, b.id === 'cancel' ? CANCEL_R : SIDE_R, now,
          'rgba(0,0,0,0.42)', 'rgba(255,255,255,0.16)', '#f0f0ff');
      }
    }

    // 底部提示：头显里没有鼠标，把当前该做什么直接写在带上
    const hint = rec ? (auto ? '停顿即自动发送 · 也可点「发送」' : (vv.hold ? '松开扳机发送' : '再看一次主按钮 = 发送'))
      : auto ? '直接说话 · 视线对准按钮 4 秒即触发'
        : '凝视按钮 4 秒即触发 · 也可按手柄扳机';
    ctx.font = '20px ' + FONT;
    ctx.fillStyle = 'rgba(240,240,255,0.6)';
    ctx.textAlign = 'left';
    ctx.fillText(fitTo(ctx, hint, TXT_R - TXT_X, '20px ' + FONT), TXT_X, 120);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 宿主接口：绘制 / 命中 / 按下 ---------------- */
  /** 宿主绘制：把带贴到宿主画布底部（dy = 宿主画布高 - bandH） */
  vv.drawBand = function drawBand(ctx: CanvasRenderingContext2D, dy: number) {
    if (!vv.cv) return;
    if (dirty) { dirty = false; draw(); }
    ctx.drawImage(vv.cv, 0, dy);
  };

  /** 宿主命中：宿主画布坐标 → 带内按钮 id（py 已由宿主减去 dy）。
   *  inside = 射线交点落在带矩形内：带内用小磁力，出带用大磁力 ——
   *  视线擦着带边缘也吸得住，但不会把远处的按钮吸过来。 */
  vv.hitBand = function hitBand(px: number, py: number, loose: boolean, inside: boolean): string | null {
    if (!vv.active || !vv.buttons.length) return null;
    let best = '';
    let bestD = Infinity;
    for (const b of vv.buttons) {
      const r = Math.min(b.w, b.h) / 2;
      const d = Math.hypot(px - (b.x + b.w / 2), py - (b.y + b.h / 2)) - r;
      if (d < bestD) { bestD = d; best = b.id; }
    }
    if (!best) return null;
    const pad = loose ? (inside ? 16 : 26) : 10;
    if (bestD <= pad) return best;
    // 带外不做吸附：射线擦着带边缘时，角落里的宽松吸附会把远处按钮吸过来
    if (!loose || !inside) return null;
    return bestD <= Math.min(MIC_R, SIDE_R) * GAZE_SNAP ? best : null;
  };

  vv.markDirty = function markDirty() { dirty = true; };

  /** 手柄扳机按下（宿主分派）：true = 已消费。
   *  press 模式的「按住说话」：按下即录，松开（selectend）发送。 */
  vv.pressDown = function pressDown(id: string): boolean {
    if (!vv.active) return false;
    if (id === 'mic' && App.voiceMode === 'press' && !recording()) {
      vv.hold = true;
      startPress();
      return true;
    }
    return false;
  };

  /* ---------------- 显示/隐藏/每帧 ---------------- */
  vv.show = function show() {
    ensureCanvas();
    vv.active = true;
    vv.hoverId = '';
    vv.focusId = '';
    vv.flash = null;
    vv.hold = false;
    buildButtons();
    dirty = true;
    maybeAutoMode();
  };

  vv.hide = function hide() {
    vv.active = false;
    vv.hoverId = '';
    vv.focusId = '';
    vv.hold = false;
    // 擦掉带画布：宿主下次进场前若先绘一帧，不会贴出上一次的残留
    if (vv.ctx) vv.ctx.clearRect(0, 0, CV_W, CV_H);
    dirty = true;
  };

  /** 宿主每帧调：状态轮询 + 手柄 selectend 补绑；返回 true = 带内容变了，宿主该重绘 */
  let lastSig = '';
  let sigAt = 0;
  vv.tick = function tick(): boolean {
    if (!vv.active) return false;
    const now = performance.now();
    if (now - sigAt > 200) {
      sigAt = now;
      const sig = App.voiceMode + '|' + (recording() ? 'R' : '-') + '|' + String(App.currentState || '');
      if (sig !== lastSig) {
        lastSig = sig;
        buildButtons();
        dirty = true;
      }
      bindSelectEnd();
    }
    if (recording()) dirty = true;   // 电平条/计时每帧都要动
    return dirty;
  };

  /** 进 VR 自动切自动对话：头显里按住说话本就不自然。
   *  只在麦克风此前已授权时切 —— 沉浸会话里弹权限窗会毁掉体验；
   *  没授权过就让用户点带上的「自动」钮（用户手势才能触发权限请求）。 */
  function maybeAutoMode() {
    if (App.voiceMode === 'auto') return;
    let stored = '';
    try { stored = localStorage.getItem('dabai.voiceMode') || ''; } catch (e) { /* 隐私模式 */ }
    if (stored === 'auto') {
      App.setVoiceMode('auto');
      return;
    }
    try {
      if (navigator.permissions && navigator.permissions.query) {
        navigator.permissions.query({ name: 'microphone' } as any).then(p => {
          if (p.state === 'granted' && App.vrVoice && App.vrVoice.active && App.voiceMode !== 'auto') {
            App.setVoiceMode('auto');
          }
        }).catch(() => { /* 不支持查询就等用户点「自动」 */ });
      }
    } catch (e) { /* 同上 */ }
  }

  /* ---------------- 命中动作 ---------------- */
  vv.trigger = function trigger(id: string) {
    if (!vv.active) return;
    vv.flash = { id, until: performance.now() + 260 };
    dirty = true;

    if (id === 'mode') {
      // 与屏幕版 #voice-btn 短按同一条管线：权限被拒时 setVoiceMode 自己回退 press
      App.setVoiceMode(App.voiceMode === 'auto' ? 'press' : 'auto');
      buildButtons();
      return;
    }

    if (id === 'cancel') {
      vv.hold = false;
      if (App.isRecording) App.stopRecording(true);
      App.showToast('已取消本次录音');
      buildButtons();
      return;
    }

    // 主按钮
    if (App.voiceMode === 'auto') {
      // 自动模式：VAD 自己录，主按钮拿来打断 AI 说话（头显里最需要的那颗键）
      if (App.currentState === 'speaking' || App.currentState === 'thinking') {
        App.triggerInterrupt(true);
        App.showToast('已打断');
      } else {
        App.showToast('自动对话中 · 直接说话即可');
      }
      return;
    }
    if (recording()) {
      vv.hold = false;
      if (App.isRecording) App.stopRecording(false);
      // 自动录音段（切模式瞬间的残留）交给 VAD 收尾
      buildButtons();
      return;
    }
    startPress();
  };

  function startPress() {
    vv.recStart = performance.now();
    dirty = true;
    App.startRecording().then(() => {
      if (!App.isRecording) return;   // 麦克风没就绪，startRecording 内部已 toast
      buildButtons();
    });
  }

  /** 手柄松开（selectend）：按住说话的发送时机。
   *  three.js 的 XR 控制器会派发 selectend，但既有代码只绑了 selectstart —— 这里补绑。 */
  function onSelectEnd() {
    if (!vv.active || !vv.hold) return;
    vv.hold = false;
    if (App.isRecording) App.stopRecording(false);
    buildButtons();
    dirty = true;
  }

  function bindSelectEnd() {
    const ctrls = App._xrControllers || [];
    for (const c of ctrls as any[]) {
      if (!c || !c.addEventListener || c.userData._vrVoiceBound) continue;
      c.userData._vrVoiceBound = true;
      c.addEventListener('selectend', onSelectEnd);
    }
  }

  /* ---------------- 与非 VR 打通 ---------------- */
  // 模式/状态被别处改（键盘快捷键、权限被拒回退）→ 立刻重绘，不必等 0.2s 轮询
  const _setMode = App.setVoiceMode;
  App.setVoiceMode = function (mode: any) {
    const r = _setMode.apply(this, arguments as any);
    buildButtons();
    dirty = true;
    return r;
  };

  const _setState = App.setState;
  if (_setState) {
    App.setState = function (s: any) {
      const r = _setState.apply(this, arguments as any);
      dirty = true;
      return r;
    };
  }
}
