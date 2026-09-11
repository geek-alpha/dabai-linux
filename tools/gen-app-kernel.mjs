#!/usr/bin/env node
/* 阶段 1 · 类型地基 —— App 命名空间属性清单提取 + AppKernel 骨架生成（一次性脚手架）
 *
 * 用法：node tools/gen-app-kernel.mjs
 * 输出：
 *   web/js/types/app-inventory.json — App 与 window 命名空间全量使用清单（读写计数 + 文件来源）
 *   web/js/types/app-kernel.ts      — AppKernel 接口（核心区占位 @@CORE@@ + 生成区）
 *
 * 说明：
 *   - 生成区类型一律 any，待各模块迁移 .ts 时精化并上移核心区；
 *   - 重新运行会重写 app-kernel.ts 并丢失核心区手工内容，运行前请先备份。
 */
import { parse } from '@babel/parser';
import { readFileSync, writeFileSync, readdirSync, statSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WEB = path.join(ROOT, 'web');
const TYPES_DIR = path.join(WEB, 'js', 'types');

/* 核心区手工维护的属性（生成时跳过；类型见 app-kernel.ts 核心区） */
const RESERVED = new Set([
  // 依赖命名空间
  'THREE', 'GLTFLoader', 'VRMLoaderPlugin', 'VRMUtils',
  // DOM 快捷引用
  '$', 'canvas', 'statusBadge', 'subtitle', 'messagesEl',
  // 状态机
  'State', 'currentState', 'setState',
  // Three.js 场景
  'scene', 'camera', 'renderer', 'modelGroup', 'currentAvatar', 'backgroundGroup',
  'DEFAULT_CAM_POS', 'targetCamPos', 'MIN_ZOOM', 'MAX_ZOOM',
  'camZoom', 'camOffsetX', 'camOffsetY', 'camOffsetZ',
  'cameraHeight', 'cameraTiltDeg', 'cameraDistance', 'gazeAssistEnabled',
  'xrMode', 'moveMode', 'backgroundAutoRotate', 'setMoveMode',
  // 场景状态持久化
  'SCENE_KEY', 'CAM_SETTINGS_KEY', '_saveTimer', '_camSettingsSaveTimer',
  'saveSceneState', 'debouncedSaveScene', 'restoreSceneState', 'loadCameraSettings', 'saveCameraSettings',
  'resetAvatarToOrigin', 'applySavedPositions', '_bgCenterX', '_bgCenterZ', '_findFloorY', '_smoothTeleport',
  // 待机漫步
  'idleWalkTarget', 'idleWalkProgress', 'walkPath', 'walkSegmentIndex', 'currentAction', 'nextActionTimer',
  // WebSocket
  'ws', 'wsHeartbeat', 'wsReconnectTimer', 'wsConnTimeout',
  'connectWS', 'handleWSMessage', 'sendText', 'sendAudioBase64',
  // RL 统一调度
  'rlHeartbeat', '_rlLastDispatchTime', '_rlStatusEl', 'sendRLSync', 'startRLHeartbeat',
  '_engagementRL', '_datingSystem', '_expressionRL', 'aiAutonomyController', 'gameModeManager',
  // 回复/音频队列
  'currentReplySession', '_interruptedSession', 'currentReplyText', 'currentReplySeg', 'audioQueue',
  'handleAudioChunk', 'handleAudioEnd', 'handleInterrupted', 'handleUsageMessage', 'clearAudioQueue',
  // 聊天 UI
  'addSystemMsg', 'addUserMsg', 'addAIMsg', 'showToast', 'showSubtitle', 'showTyping', 'removeTyping',
  'scrollToBottom', '_trimMessages', 'notifyFullscreenChat',
  // 语音/VAD
  'voiceMode', 'vadStream', 'vadState', 'vadLoop', 'startVADMode', 'stopVADMode',
  'triggerInterrupt', 'setVoiceMode', 'lockMode', 'toggleLockMode',
  // 工具链卡片
  'toolChainBeginTurn', 'toolChainStart', 'toolChainResult', 'toolChainAbort',
  'addToolCallMsg', 'addToolCallResult',
  // 会话管理
  'sessionModalEl', 'sessionListEl', 'sessionBtn', 'sessionModalClose', 'newSessionBtn',
  'initSessionUI', 'renderSessionList', 'renderSessionListFromState',
  // DSH 桥接
  'harnessRequestId', 'harnessStatus', '_harnessPollTimer', '_harnessPolling',
  'showHarnessConfirm', 'harnessPoll', 'updateHarnessStatus', 'harnessApprove', 'harnessClose',
  'onBridgeSay', 'dshCardExists', 'notifyTaskDeclined',
  // 任务中心
  'handleTaskEvent', 'taskBoardOnEvent', 'dshCardOnEvent', 'addTaskTreeMsg', 'maybeRenderTaskTree',
  // 屏幕控制/媒体
  'handleScreenCommand', 'fuzzyMatchFile', 'loadModelFromUrl', 'loadBackgroundFromUrl', 'useDefaultBackground',
  'switchTTSEngine', 'setBGMVolume', 'playMusicTrack', 'stopBGM',
  'playPlaylistCmd', 'videoBoardPlay', 'videoBoardControl',
  // 唤醒词/活跃度
  'wakeWords', 'onWakeOk', 'onWakeFail', 'bumpConversation',
  '_lastUserMessageTime', '_lastUserInteractTime', '_sentAvatarName', '_sentBgName', 'MOTION_LIBRARY',
]);

function walkJs(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'types' || name.startsWith('.')) continue;
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) walkJs(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

const files = walkJs(WEB).sort();
const appProps = new Map();     // name -> Map(file -> {reads, writes})
const windowProps = new Map();

function record(map, name, file, isWrite) {
  let perFile = map.get(name);
  if (!perFile) { perFile = new Map(); map.set(name, perFile); }
  let c = perFile.get(file);
  if (!c) { c = { reads: 0, writes: 0 }; perFile.set(file, c); }
  if (isWrite) c.writes++; else c.reads++;
}

const SKIP_KEYS = new Set(['loc', 'leadingComments', 'trailingComments', 'innerComments', 'extra']);

function visit(node, parent, file) {
  if (!node || typeof node.type !== 'string') return;
  if (node.type === 'MemberExpression' && !node.computed
    && node.object.type === 'Identifier' && node.property.type === 'Identifier') {
    const holder = node.object.name;
    if (holder === 'App' || holder === 'window') {
      const isWrite = !!parent && (
        (parent.type === 'AssignmentExpression' && parent.left === node) ||
        (parent.type === 'UpdateExpression' && parent.argument === node));
      record(holder === 'App' ? appProps : windowProps, node.property.name, file, isWrite);
    }
  } else if (node.type === 'VariableDeclarator' && node.init && node.init.type === 'Identifier'
    && node.init.name === 'App' && node.id.type === 'ObjectPattern') {
    for (const p of node.id.properties) {
      if (p.type === 'Property' && p.key.type === 'Identifier') record(appProps, p.key.name, file, false);
    }
  }
  for (const key of Object.keys(node)) {
    if (SKIP_KEYS.has(key)) continue;
    const v = node[key];
    if (Array.isArray(v)) {
      for (const c of v) if (c && typeof c.type === 'string') visit(c, node, file);
    } else if (v && typeof v.type === 'string') {
      visit(v, node, file);
    }
  }
}

const parseFailures = [];
for (const f of files) {
  try {
    const ast = parse(readFileSync(f, 'utf8'), { sourceType: 'module' });
    visit(ast.program, null, f);
  } catch (e) {
    parseFailures.push(path.relative(ROOT, f) + ' :: ' + e.message);
  }
}

const rel = f => path.relative(WEB, f).replace(/\\/g, '/');

function serialize(map) {
  return [...map.entries()].map(([name, perFile]) => {
    const fs = [...perFile.entries()].map(([f, c]) => ({ file: rel(f), reads: c.reads, writes: c.writes }));
    return {
      name,
      reads: fs.reduce((s, x) => s + x.reads, 0),
      writes: fs.reduce((s, x) => s + x.writes, 0),
      files: fs.sort((a, b) => (b.writes - a.writes) || (b.reads - a.reads)),
    };
  }).sort((a, b) => (b.reads + b.writes) - (a.reads + a.writes));
}

const inventory = {
  generatedAt: new Date().toISOString(),
  scannedFiles: files.length,
  parseFailures,
  appPropCount: appProps.size,
  windowPropCount: windowProps.size,
  appProps: serialize(appProps),
  windowProps: serialize(windowProps),
};
mkdirSync(TYPES_DIR, { recursive: true });
writeFileSync(path.join(TYPES_DIR, 'app-inventory.json'), JSON.stringify(inventory, null, 2) + '\n');

// 生成区分组：按「写入最多的文件」归类
const groups = new Map();
for (const [name, perFile] of appProps) {
  if (RESERVED.has(name)) continue;
  const fs = [...perFile.entries()].sort((a, b) => (b[1].writes - a[1].writes) || a[0].localeCompare(b[0]));
  const primary = rel(fs[0][0]);
  if (!groups.has(primary)) groups.set(primary, []);
  groups.get(primary).push(name);
}

const genLines = [];
for (const f of [...groups.keys()].sort()) {
  genLines.push(`  /* ---- ${f} ---- */`);
  for (const n of groups.get(f).sort()) genLines.push(`  ${n}?: any;`);
  genLines.push('');
}

const kernel = `/* ============================================================
 * App 内核类型 —— 阶段 1（类型地基）
 * ------------------------------------------------------------
 * App 是贯穿全部模块的内核对象（web/app.js 逐个调用各模块的
 * init(App) 挂载属性）。本文件是它的类型总账：
 *
 *   核心区（CORE 下方）      —— 手工维护，类型精确
 *   生成区（GENERATED 下方） —— tools/gen-app-kernel.mjs 扫描
 *     web 目录全部 JS 文件的 App 属性用法自动生成，类型暂为 any，
 *     各模块迁移 .ts 时逐步精化并上移到核心区
 *
 * 全量使用清单（读写计数 + 文件来源）见同目录 app-inventory.json。
 * ============================================================ */

import type * as ThreeNS from 'three';
import type { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import type { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import type {
  AudioChunkMessage,
  BridgeStatusMessage,
  ScreenCommandArgs,
  ScreenCommandMessage,
  ServerMessage,
  SessionSummary,
} from './ws-protocol.js';

/** 角色状态机取值 */
export interface AppKernelState {
  IDLE: 'idle';
  THINKING: 'thinking';
  LISTENING: 'listening';
  SPEAKING: 'speaking';
}

/** localStorage 持久化的场景状态（window._restoredScene 同构） */
export interface ScenePersistState {
  camZoom?: number;
  camOffsetX?: number;
  camOffsetY?: number;
  camOffsetZ?: number;
  xrMode?: boolean;
  moveMode?: boolean;
  backgroundAutoRotate?: boolean;
  charPos?: { x: number; z: number };
  charScale?: number;
  bgPos?: { x: number; z: number };
  bgScale?: number;
}

export interface AppKernel {
  /* @@CORE@@ —— 手工维护区：已迁移 .ts 的模块所用属性，类型精确 */

  /* ============================================================
   * GENERATED —— 生成区（勿手改）：类型 any，待各模块迁移时精化
   * ============================================================ */
${genLines.join('\n')}}
`;

writeFileSync(path.join(TYPES_DIR, 'app-kernel.ts'), kernel);

const generatedCount = [...appProps.keys()].filter(n => !RESERVED.has(n)).length;
console.log(`scanned ${files.length} files`);
console.log(`App props: ${appProps.size} (core reserved: ${RESERVED.size}, generated: ${generatedCount})`);
console.log(`window props: ${windowProps.size}`);
console.log(`parse failures: ${parseFailures.length}`);
if (parseFailures.length) console.log(parseFailures.join('\n'));
