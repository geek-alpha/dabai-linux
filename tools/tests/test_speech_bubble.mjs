/**
 * 头顶气泡状态机回归测试（node 直跑，无浏览器）
 * 覆盖：突发合并 / 同句去重 / 乱序丢弃 / 会话切换重置 / 单实例保证 / 隐藏丢弃待绘制 / 布局上界 / 布局缓存
 * 做法：用 node:module.stripTypeScriptTypes 复刻 server.py 的 .ts 直服路径，再喂假 App + 假 canvas。
 *
 * 用法：node tools/tests/test_speech_bubble.mjs      （退出码 0=全过，1=有失败）
 * 依赖：Node ≥ 22（需要 node:module.stripTypeScriptTypes）
 * 维护：改了 web/js/character/07_click_interact.ts 的气泡/布局逻辑后必跑本脚本。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stripTypeScriptTypes } from 'node:module';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const SRC = path.join(ROOT, 'web', 'js', 'character', '07_click_interact.ts');

/* ---------- 假 DOM：canvas 只记录关键调用 ---------- */
let measureCalls = 0;
function makeCanvas() {
  const cv = { width: 0, height: 0, getContext: () => ctx };
  const ctx = {
    font: '', textAlign: '', textBaseline: '', fillStyle: '', strokeStyle: '',
    shadowColor: '', shadowBlur: 0, lineWidth: 1,
    measureText(s) {
      measureCalls++;
      const m = /(\d+)px/.exec(ctx.font);
      const px = m ? Number(m[1]) : 56;
      return { width: String(s).length * px * 0.95 };
    },
    save() {}, restore() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    arcTo() {}, closePath() {}, fill() {}, stroke() {}, fillText() {}
  };
  return cv;
}
globalThis.document = { createElement: (tag) => (tag === 'canvas' ? makeCanvas() : {}) };

/* ---------- 假 THREE ---------- */
let uploads = 0;
class Vector3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
  copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; }
  sub(v) { this.x -= v.x; this.y -= v.y; this.z -= v.z; return this; }
  length() { return Math.sqrt(this.x * this.x + this.y * this.y + this.z * this.z); }
}
class SpriteMaterial {
  constructor(o = {}) { Object.assign(this, o); }
  dispose() {}
}
class CanvasTexture {
  constructor(img) { this._img = img; this.version = 0; this._needs = false; }
  get image() { return this._img; }
  set image(v) { this._img = v; }
  get needsUpdate() { return this._needs; }
  set needsUpdate(v) { this._needs = !!v; if (v) uploads++; }
  dispose() {}
}
class Sprite {
  constructor(mat) {
    this.material = mat; this.userData = {}; this.visible = false; this.parent = null;
    this.isSprite = true; this.renderOrder = 0;
    this.position = new Vector3();
    this.scale = { set() {} };
  }
  raycast() {}
}
const THREE = { Vector3, Sprite, SpriteMaterial, CanvasTexture, LinearFilter: 1006 };

/* ---------- 假 App ---------- */
const App = {
  THREE,
  modelGroup: {
    children: [],
    add(o) { o.parent = this; this.children.push(o); },
    remove(o) { const i = this.children.indexOf(o); if (i >= 0) this.children.splice(i, 1); o.parent = null; },
    worldToLocal(v) { return v; }
  },
  camera: { position: new Vector3(0, 1.6, 3) },
  xrPresenting: false,
  headBone: null,
  State: { SPEAKING: 'speaking' },
  parts: {},
  currentState: 'speaking'
};

/* ---------- 复刻 server.py 的 .ts 直服转译 ---------- */
const js = stripTypeScriptTypes(fs.readFileSync(SRC, 'utf8'));
const tmp = path.join(os.tmpdir(), 'dabai_bubble_mod_' + Date.now() + '.mjs');
fs.writeFileSync(tmp, js);
const mod = await import('file://' + tmp);
mod.default(App);

/* ---------- 计数包装 ---------- */
let draws = 0;
const realDraw = App._drawChatBubbleCanvas;
App._drawChatBubbleCanvas = (t) => { draws++; return realDraw(t); };

let pass = 0, fail = 0;
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log('  ✅', name, extra); }
  else { fail++; console.log('  ❌', name, extra); }
}
const show = (t, seq, sess) => App.showChatBubble(t, seq, sess);
const frame = () => App.updateSpeechBubble();
const bubbleSprites = () => App.modelGroup.children.filter(
  (c) => c && c.isSprite && c.userData && c.userData.__dabaiBubble);

console.log('— T1 同帧突发 50 次调用 → 只重绘 1 次、只上传 1 次');
for (let i = 1; i <= 50; i++) show('分句' + i);
frame();
ok('绘制次数 = 1', draws === 1, '实际 ' + draws);
ok('纹理上传 = 1', uploads === 1, '实际 ' + uploads);
ok('显示的是最后一句', App._speechBubbleText === '分句50', App._speechBubbleText);
ok('场景内气泡 Sprite 唯一', bubbleSprites().length === 1, '实际 ' + bubbleSprites().length);

console.log('— T2 同句重复调用 → 不重绘，只续期');
const d0 = draws;
show('分句50');
frame();
ok('无多余重绘', draws === d0, 'draws ' + d0 + '→' + draws);

console.log('— T3 乱序/过期分句被丢弃，会话切换重置序号');
show('s1-1', 1, 'sess-A');
show('s1-2', 2, 'sess-A');
frame();
ok('顺序分句正常上屏', App._speechBubbleText === 's1-2', App._speechBubbleText);
show('s1-1迟到', 1, 'sess-A');
frame();
ok('迟到旧序号被丢弃', App._speechBubbleText === 's1-2', App._speechBubbleText);
show('s1-3', 3, 'sess-A');
frame();
ok('新序号正常上屏', App._speechBubbleText === 's1-3', App._speechBubbleText);
show('s2-1', 1, 'sess-B');
frame();
ok('换会话后 seq=1 不被误杀', App._speechBubbleText === 's2-1', App._speechBubbleText);

console.log('— T4 残留气泡被清理（单实例强保证）');
const stale = {
  isSprite: true, userData: { __dabaiBubble: true }, parent: null,
  material: { map: { dispose() {} }, dispose() {} }
};
App.modelGroup.children.push(stale);
show('单实例', 9, 'sess-B');
frame();
ok('残留气泡已摘除', bubbleSprites().length === 1 && !App.modelGroup.children.includes(stale),
  '实际 ' + bubbleSprites().length);

console.log('— T5 隐藏后待绘制台词不再冒出来');
const d1 = draws;
show('不该出现');
App.hideChatBubble();
frame();
ok('隐藏后不重绘', draws === d1, 'draws ' + d1 + '→' + draws);
ok('隐藏后无台词残留', App._speechBubbleText === '' && App._speechBubblePending == null);

console.log('— T6 布局上界：4 行截断 + 行高 > 字号 + 测量次数有界');
const longText = '这是一段很长的台词用来压测换行与字号自适应'.repeat(80);
measureCalls = 0;
const layout = App._measureBubbleLayout(longText);
ok('行数 ≤ 4', layout.lines.length <= 4, '实际 ' + layout.lines.length);
ok('行高 > 字号（不会叠字）', layout.lineH > layout.font, layout.font + 'px / 行高 ' + layout.lineH);
ok('省略号截断', layout.lines[layout.lines.length - 1].endsWith('…'));
ok('测量次数有界（≤5 轮 × 400 字）', measureCalls <= 5 * 400, '实际 ' + measureCalls);

console.log('— T7 布局缓存命中（重复台词零测量）');
const l1 = App._measureBubbleLayout('缓存命中测试');
const afterFirst = measureCalls;
const l2 = App._measureBubbleLayout('缓存命中测试');
ok('命中同一布局对象', l1 === l2);
ok('第二次零额外测量', measureCalls === afterFirst, '实际 +' + (measureCalls - afterFirst));

console.log('\n结果：' + pass + ' 通过 / ' + fail + ' 失败');
fs.unlinkSync(tmp);
process.exit(fail ? 1 : 0);
