/* ============================================================
 * 3D 虚拟 AI 角色陪聊 · App 内核对象
 *
 * App 是贯穿全部模块的内核对象：web/app.ts 逐个调用各模块的
 * init(App)，模块把属性/方法挂到 App 上。类型总账见
 * js/types/app-kernel.ts（核心区手工精确类型 + 生成区待精化）。
 * ============================================================ */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import type { AppKernel } from '../types/app-kernel.js';

/* 其余属性由 web/app.ts 依序调用各模块 init(App) 挂载，
 * 运行时在首次使用前均已就绪，这里以断言收口。 */
export const App = {
  THREE,
  GLTFLoader,
  VRMLoaderPlugin,
  VRMUtils,
} as AppKernel;
