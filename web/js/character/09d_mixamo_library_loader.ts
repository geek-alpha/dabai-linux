import type { AppKernel, MixamoClipInfo, AnimLibraryConfig, AnimCategory } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  Mixamo 动作库加载器 —— 批量加载 + 情绪映射 + 分类索引
   *
   *  定位：在 mixamo_retarget 基础上的"库管理层"。
   *  - 从 animation-library.json 读取动作元数据
   *  - 批量加载 FBX 并重定向注册到 App.mixamoClips
   *  - 提供按情绪/分类/名称的播放 API
   *  - 与情绪控制器联动：情绪变化时自动切换待机动作
   *
   *  用法：
   *    // 加载整个库（VRM 加载完成后调用）
   *    await App.loadAnimationLibrary()
   *
   *    // 按名称播放
   *    App.playLibraryClip('laugh')
   *
   *    // 按情绪随机播放一个动作
   *    App.playEmotionClip('happy')
   *
   *    // 按分类随机播放
   *    App.playCategoryClip('dance')
   * ============================================================ */

  // ==================== 运行时状态 ====================
  App._animLibraryConfig = null;
  App._animLibraryLoaded = false;
  App._animLibraryLoading = false;
  App._animLibraryStats = { total: 0, loaded: 0, failed: 0 };
  App._animLibraryGen = 0; // 模型切换代数：加载过程中换模型则中止旧加载
  // 角色专属动作配置：null = 未配置/关闭 → 执行全部动作
  App._roleAnimationConfig = null;
  // —— 动态加载 + LRU 缓存池 ——
  App._animCacheMax = 10;        // 缓存池容量：只保留最近用过的 N 个动作，超出销毁最久未用的
  App._animLRU = [];             // 最近使用顺序（尾部最新）
  App._animLoading = new Set();  // 正在加载的动作名（防重复加载）
  App._animPrefetchTimer = null; // 预取定时器：播放后异步预取下一个，切换时已就绪

  /**
   * 重置动作库状态（模型销毁/切换时调用，避免旧模型的片段残留）
   */
  App.resetAnimationLibrary = function resetAnimationLibrary() {
    App._animLibraryGen++;
    App.mixamoClips = {};
    if (App.mixamoMixer) {
      App.mixamoMixer.stopAllAction();
      App.mixamoMixer = null;
    }
    App._mixamoActiveClip = null;
    App._mixamoTailActive = false;
    App._mixamoTailName = null;
    App._mixamoTailRem = 0;
    App._mixamoTailTotal = 0;
    App._animLibraryConfig = null;
    App._animLibraryLoaded = false;
    App._animLibraryLoading = false;
    App._animLibraryStats = { total: 0, loaded: 0, failed: 0 };
    App._mixamoActiveClipLoop = false;
    App._mixamoActiveClipStart = 0;
    App._mixamoSwitchTimer = null;
    App._mixamoEmotionEnabled = false;
    App._mixamoHipsRestPos = null; // 模型切换 → 旧模型的 hips 静息位失效，下次加载片段时重新捕获
    App.clearAnimState(); // 模型切换 → 上报无动作
    App._mixamoLastEmotion = 'neutral';
    App._mixamoEmotionCooldown = 0;
    // 清空动态加载池
    App._animLRU = [];
    App._animLoading = new Set();
    if (App._animPrefetchTimer) { clearTimeout(App._animPrefetchTimer); App._animPrefetchTimer = null; }
  };

  // ==================== 动态加载 + LRU 缓存池 ====================
  /**
   * 确保动作已加载：已缓存直接命中；未缓存则异步加载并进入 LRU 池。
   * 池超容量时销毁最久未用的动作（从库删除 + mixer 解绑，释放内存）。
   */
  App.ensureClipLoaded = async function ensureClipLoaded(name: string): Promise<boolean> {
    if (App.mixamoClips[name]) { App.touchLRU(name); return true; }
    if (App._animLoading.has(name)) return false; // 已在加载中，避免并发重复加载
    const config = App._animLibraryConfig;
    if (!config) return false;
    let animDef: any = null;
    for (const key in config.categories) {
      const found = config.categories[key].animations.find((a: any) => a.name === name);
      if (found) { animDef = found; break; }
    }
    if (!animDef) return false;
    App._animLoading.add(name);
    try {
      // 优先烘焙缓存（秒开零重定向）；无缓存回退 FBX 实时重定向
      const bakedUrl = '/anim/baked/' + name + '.json';
      let clip = await App.loadBakedMixamoClip(name, bakedUrl);
      if (!clip) {
        const baseUrl = config.baseUrl || '/anim/';
        clip = await App.loadMixamoAnimation(baseUrl + animDef.file, name);
      }
      if (clip && App.mixamoClips[name]) {
        App.mixamoClips[name].emotion = animDef.emotion;
        App.mixamoClips[name].loop = animDef.loop ?? false;
        App.touchLRU(name);
        return true;
      }
      return false;
    } catch (e) {
      console.warn('[AnimLib] 动态加载失败:', name, e);
      return false;
    } finally {
      App._animLoading.delete(name);
    }
  };

  /** 触碰 LRU（标记最近使用），超容量销毁最久未用的 */
  App.touchLRU = function touchLRU(name: string) {
    const idx = App._animLRU.indexOf(name);
    if (idx >= 0) App._animLRU.splice(idx, 1);
    App._animLRU.push(name);
    // 淘汰：从最久未用开始；正在播放的与 idle 常驻动作（兜底待机）永不淘汰
    while (App._animLRU.length > App._animCacheMax) {
      const victim = App._animLRU.shift();
      if (!victim) break;
      if (victim === App._mixamoActiveClip || animCategoryOf(victim) === 'idle') {
        App._animLRU.unshift(victim); // 放回队首保持最久未用位置，停止淘汰
        break;
      }
      App.destroyClip(victim);
    }
  };

/** 销毁动作：从库删除 + 从 mixer 解绑，释放内存（正在播放的不销毁） */
  App.destroyClip = function destroyClip(name: string) {
    const info = App.mixamoClips[name];
    if (!info) return;
    if (App._mixamoActiveClip === name) return;
    try {
      if (App.mixamoMixer) App.mixamoMixer.uncacheClip(info.clip);
    } catch (e) { /* 解绑失败不影响删除 */ }
    delete App.mixamoClips[name];
    const idx = App._animLRU.indexOf(name);
    if (idx >= 0) App._animLRU.splice(idx, 1);
    console.log('[AnimLib] 销毁动作(缓存淘汰):', name);
  };

  /** 播放成功后异步预取一个未加载动作，下次切换已就绪 */
  App._prefetchNext = function _prefetchNext() {
    if (App._animPrefetchTimer) { clearTimeout(App._animPrefetchTimer); App._animPrefetchTimer = null; }
    App._animPrefetchTimer = setTimeout(() => {
      App._animPrefetchTimer = null;
      const config = App._animLibraryConfig;
      if (!config) return;
      const allDefs: any[] = [];
      for (const key in config.categories) {
        if (key === 'walk') continue; // walk 由移动系统负责，不预取
        for (const a of config.categories[key].animations) {
          if (App.isAnimAllowed(a.name)) allDefs.push(a);
        }
      }
      if (!allDefs.length) return;
      const unloaded = allDefs.filter(a => !App.mixamoClips[a.name] && !App._animLoading.has(a.name));
      if (!unloaded.length) return;
      const target = unloaded[Math.floor(Math.random() * unloaded.length)];
      App.ensureClipLoaded(target.name);
    }, 800); // 避开当前帧峰值，播稳后再预取
  };

  // ==================== 角色专属动作过滤 ====================
  /**
   * 设置当前角色的专属动作配置（由角色卡片应用时调用）。
   * @param config  { enabled, allowed }；enabled=false 或 allowed 为空 → 执行全部动作
   */
  App.setRoleAnimationConfig = function setRoleAnimationConfig(config) {
    if (config && config.enabled && Array.isArray(config.allowed) && config.allowed.length > 0) {
      App._roleAnimationConfig = { enabled: true, allowed: [...config.allowed] };
    } else {
      App._roleAnimationConfig = null;
    }
  };

  /**
   * 判断动作是否允许当前角色执行（未配置专属动作时全部允许）
   */
  App.isAnimAllowed = function isAnimAllowed(name: string): boolean {
    if (!App._roleAnimationConfig) return true;
    return App._roleAnimationConfig.allowed.includes(name);
  };

  /**
   * 返回当前角色允许的所有已加载动作信息
   */
  App.getAllowedClips = function getAllowedClips(): any[] {
    return Object.values(App.mixamoClips).filter(c => App.isAnimAllowed(c.name));
  };

  /**
   * 从允许的动作中随机选一个循环动作（用于待机），优先情绪匹配
   */
  App.pickAllowedLoopClip = function pickAllowedLoopClip(preferEmotion?: string): string | null {
    const allowed = App.getAllowedClips().filter(c => c.loop);
    if (!allowed.length) return null;
    if (preferEmotion) {
      const match = allowed.filter(c => c.emotion === preferEmotion);
      if (match.length) return match[Math.floor(Math.random() * match.length)].name;
    }
    return allowed[Math.floor(Math.random() * allowed.length)].name;
  };

  /**
   * 从允许的动作中按情绪选一个表达动作（无匹配返回 null）
   */
  App.pickAllowedClipByEmotion = function pickAllowedClipByEmotion(emotion: string): string | null {
    const allowed = App.getAllowedClips();
    if (!allowed.length) return null;
    const match = allowed.filter(c => c.emotion === emotion);
    if (match.length) return match[Math.floor(Math.random() * match.length)].name;
    return null;
  };

  // ==================== 配置加载 ====================
  /**
   * 加载动作库配置文件
   */
  App.loadAnimLibraryConfig = async function loadAnimLibraryConfig(): Promise<AnimLibraryConfig | null> {
    if (App._animLibraryConfig) return App._animLibraryConfig;
    try {
      const res = await fetch('/anim/animation-library.json');
      if (!res.ok) {
        console.warn('[AnimLib] 配置文件加载失败:', res.status);
        return null;
      }
      const config = await res.json();
      App._animLibraryConfig = config;
      console.log(`[AnimLib] 配置加载完成，共 ${countTotalAnimations(config)} 个动作`);
      return config;
    } catch (e) {
      console.error('[AnimLib] 配置加载异常:', e);
      return null;
    }
  };

  /**
   * 统计配置中的动作总数
   */
  function countTotalAnimations(config: AnimLibraryConfig): number {
    let total = 0;
    for (const key in config.categories) {
      total += config.categories[key].animations.length;
    }
    return total;
  }

  // ==================== 批量加载 ====================
  /**
   * 加载单个动作：优先烘焙缓存（/anim/baked/<name>.json，秒开零重定向），
   * 无烘焙缓存或加载失败时回退 FBX 实时重定向。
   */
  async function loadClip(anim: { name: string; file: string }, fbxUrl: string) {
    const bakedUrl = '/anim/baked/' + anim.name + '.json';
    const baked = await App.loadBakedMixamoClip(anim.name, bakedUrl);
    if (baked) return baked;
    return App.loadMixamoAnimation(fbxUrl, anim.name);
  }
  /**
   * 加载动作库配置 + 预载待机动作（VRM 加载完成后调用）。
   * 不再全量预加载 73 个动作——改为「配置就绪 + 动态按需加载 + LRU 缓存池」：
   * 启动只预载 idle 分类几个循环动作撑场，其余动作播放时动态加载、
   * 播完淘汰销毁，避免启动期与运行期的大批量解析卡顿。
   * @param lazy  保留参数（兼容调用方），动态模式下忽略
   */
  App.loadAnimationLibrary = async function loadAnimationLibrary(lazy = false): Promise<number> {
    if (App._animLibraryLoaded || App._animLibraryLoading) {
      return Object.keys(App.mixamoClips).length;
    }
    App._animLibraryLoading = true;
    const gen = App._animLibraryGen; // 记录当前模型代数，加载中途换模型则中止

    const config = await App.loadAnimLibraryConfig();
    if (!config) {
      App._animLibraryLoading = false;
      return 0;
    }

    const baseUrl = config.baseUrl || '/anim/';
    const allAnims: { name: string; file: string; emotion?: string; loop?: boolean }[] = [];
    for (const catKey in config.categories) {
      const cat = config.categories[catKey];
      for (const anim of cat.animations) {
        allAnims.push(anim);
      }
    }
    App._animLibraryStats = { total: allAnims.length, loaded: 0, failed: 0 };

    // 启动预载：只加载 idle 分类（循环待机，撑住开场），其余动态按需加载
    const toLoad = (config.categories['idle']?.animations || []).slice(0, 4);
    for (const anim of toLoad) {
      if (App._animLibraryGen !== gen) {
        console.warn('[AnimLib] 模型已切换，中止动作库加载');
        App._animLibraryLoading = false;
        return App._animLibraryStats.loaded;
      }
      try {
        const url = baseUrl + anim.file;
        let clip = await loadClip(anim, url);
        if (clip && App.mixamoClips[anim.name]) {
          App.mixamoClips[anim.name].emotion = anim.emotion;
          App.mixamoClips[anim.name].loop = anim.loop ?? false;
          App.touchLRU(anim.name);
          App._animLibraryStats.loaded++;
        } else {
          App._animLibraryStats.failed++;
        }
      } catch (e) {
        App._animLibraryStats.failed++;
      }
    }

    // 配置就绪即视为「库可用」：后续动作全部动态加载（LRU 池管理）
    App._animLibraryLoaded = true;
    App._animLibraryLoading = false;
    console.log(`[AnimLib] 动态模式就绪: 预载 ${App._animLibraryStats.loaded} 个待机, 共 ${App._animLibraryStats.total} 个动作按需加载`);
    return App._animLibraryStats.loaded;
  };

  // ==================== 播放 API ====================
  /**
   * 播放库中的动作片段
   * @param name  动作名称
   * @param opts  播放选项
   */
  App.playLibraryClip = function playLibraryClip(name: string, opts?: any): boolean {
    if (!App.isAnimAllowed(name)) {
      console.warn(`[AnimLib] 动作 ${name} 不在当前角色专属动作列表，已拦截`);
      return false;
    }
    const info = App.mixamoClips[name];
    if (!info) {
      console.warn(`[AnimLib] 动作不存在: ${name}`);
      return false;
    }
    const loop = opts?.loop ?? info.loop ?? false;
    App.playMixamoClip(name, { ...opts, loop });
    App.updateAnimState(name); // 上报当前动作（说话时大白知道自己在做什么）
    return true;
  };

  /**
   * 按情绪随机播放一个关联动作
   * @param emotion  情绪标签
   * @param opts     播放选项
   */
  App.playEmotionClip = function playEmotionClip(emotion: string, opts?: any): string | null {
    const config = App._animLibraryConfig;
    if (!config?.emotionMap) return null;

    const pool = config.emotionMap[emotion];
    if (!pool || pool.length === 0) return null;

    // 筛选已加载且当前角色允许的动作
    const available = pool.filter(name => App.mixamoClips[name] && App.isAnimAllowed(name));
    if (available.length === 0) {
      console.warn(`[AnimLib] 情绪 ${emotion} 没有当前角色允许的动作`);
      return null;
    }

    // 强度分层：emotionMap 数组按 [爆发 → 手势 → 待机] 排序，
    // 按当前唤醒度选段——高唤醒偏爆发动作，低唤醒偏安静待机，中段偏手势表达。
    const arousal = (App.pad && App.pad.arousal) || 0.5;
    const n = available.length;
    let slice;
    if (arousal > 0.6) {
      slice = available.slice(0, Math.max(1, Math.ceil(n * 0.4)));
    } else if (arousal > 0.3) {
      slice = available.slice(Math.floor(n * 0.3), Math.max(1, Math.ceil(n * 0.7)));
    } else {
      slice = available.slice(Math.floor(n * 0.6));
    }
    const name = slice[Math.floor(Math.random() * slice.length)];
    App.playLibraryClip(name, opts);
    return name;
  };

  /**
   * 按分类随机播放一个动作
   * @param category  分类名（idle/gesture/emotion/walk/dance/pose）
   * @param opts      播放选项
   */
  App.playCategoryClip = function playCategoryClip(category: string, opts?: any): string | null {
    const config = App._animLibraryConfig;
    if (!config?.categories?.[category]) return null;

    const cat = config.categories[category];
    const available = cat.animations.filter(a => App.mixamoClips[a.name] && App.isAnimAllowed(a.name));
    if (available.length === 0) return null;

    const anim = available[Math.floor(Math.random() * available.length)];
    App.playLibraryClip(anim.name, opts);
    return anim.name;
  };

  /**
   * 获取指定分类下所有可用动作名
   */
  App.getCategoryClips = function getCategoryClips(category: string): string[] {
    const config = App._animLibraryConfig;
    if (!config?.categories?.[category]) return [];
    return config.categories[category].animations
      .filter(a => App.mixamoClips[a.name] && App.isAnimAllowed(a.name))
      .map(a => a.name);
  };

  /**
   * 获取指定情绪关联的所有可用动作名
   */
  App.getEmotionClips = function getEmotionClips(emotion: string): string[] {
    const config = App._animLibraryConfig;
    if (!config?.emotionMap?.[emotion]) return [];
    return config.emotionMap[emotion].filter(name => App.mixamoClips[name] && App.isAnimAllowed(name));
  };

  /**
   * 获取加载统计
   */
  App.getAnimLibraryStats = function getAnimLibraryStats() {
    return { ...App._animLibraryStats, available: Object.keys(App.mixamoClips).length };
  };

  // ==================== 统一动作调度 ====================
  // 统一规则：当前情绪 + 场景分类 → 在该分类的在盘动作里随机挑一个播放。
  // - 场景分类按情绪应景偏好选择（LIBRARY_EMOTION_SCENES：情绪→候选分类，越靠前越应景）；
  // - 分类内优先情绪匹配的子池，无匹配再整类随机；并避免与上一次连续重复；
  // - 与手写程序式动作（POSE/WALK/TURN/DANCE）共用同一个调度口：
  //   库可用时优先播在盘动作，库未加载或无可用动作时由调用方回退程序式动作；
  // - walk 分类可由外部显式指定（pickLibraryActionByScene('walk')），但自主轮换
  //   默认不选 walk：FBX 行走带位移轨道，原地播放会让角色滑步，行走应景由
  //   既有的移动系统（程序式走路）负责。
  App._lastScheduledClip = null; // 防连续重复：上次统一调度播放的动作名

  // ==================== 动作状态上报 ====================
  // 前端每次播放/停止库动作时维护当前动作状态，并节流上报后端（anim_state 消息）；
  // 后端在生成回复时注入 LLM 上下文（【你现在的动作】），
  // 大白说话时就知道自己正在做什么动作，回复内容可以自然地配合当前动作。
  App._currentAnimState = null;       // 当前动作 { name, category, emotion }
  App._lastAnimAnnounce = 0;          // 上报节流时间戳（600ms 防抖）

  function animCategoryOf(name: string): string {
    const config = App._animLibraryConfig;
    if (!config || !config.categories) return '';
    for (const key in config.categories) {
      const cat = config.categories[key];
      if (cat.animations.some(a => a.name === name)) return key;
    }
    return '';
  }

  /** 更新当前动作状态并触发上报（分类与情绪自动从动作库配置补齐） */
  App.updateAnimState = function updateAnimState(name: string) {
    const info = App.mixamoClips[name];
    App._currentAnimState = {
      name,
      category: animCategoryOf(name),
      emotion: (info && info.emotion) || ''
    };
    App._announceAnimState();
  };

  /** 清空当前动作状态并上报（动作停止/模型切换时） */
  App.clearAnimState = function clearAnimState() {
    App._currentAnimState = null;
    App._announceAnimState();
  };

  /** 节流上报：动作变化时同步给后端，1.5s 内合并 */
  App._announceAnimState = function _announceAnimState() {
    const now = performance.now();
    if (now - App._lastAnimAnnounce < 600) return;
    App._lastAnimAnnounce = now;
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    const st = App._currentAnimState;
    try {
      App.ws.send(JSON.stringify({
        type: 'anim_state',
        anim: st ? { name: st.name, category: st.category, emotion: st.emotion } : null
      }));
    } catch (e) {
      // 上报失败不影响动作播放
    }
  };

  // 情绪 → 应景场景分类候选（越靠前越应景；不指定场景时按当前情绪在此随机选一个）
  App.LIBRARY_EMOTION_SCENES = {
    happy:      ['idle', 'gesture', 'emotion', 'dance'],
    excited:    ['gesture', 'emotion', 'dance'],
    sad:        ['idle', 'pose', 'emotion'],
    angry:      ['idle', 'gesture'],
    surprised:  ['gesture', 'emotion'],
    shy:        ['idle', 'pose', 'gesture', 'emotion'],
    thoughtful: ['idle', 'gesture'],
    tired:      ['pose', 'idle', 'emotion'],
    calm:       ['pose', 'idle'],
    proud:      ['pose', 'gesture'],
    playful:    ['gesture', 'dance', 'emotion'],
    love:       ['gesture', 'emotion'],
    neutral:    ['idle', 'pose', 'gesture', 'emotion', 'dance']
  };

  // 各场景循环动作的播放时长上限（秒区间）：循环动作播够时间就释放回程序式动作，
  // 避免一个循环动作永久霸占全身骨骼。
  // 注意：这里只作为“超长兜底”，正常轮换已由调度器按 clip 完整时长控制——
  // 之前限时 3~6s 与调度器双保险一起掐，导致舞蹈/姿势等长动作永远播不完整。
  // 现在放宽到 15~25s：只拦超长动作（如 phone_call 37s），常规动作自然播完。
  App.LIBRARY_SCENE_HOLD = {
    idle: [15, 25], pose: [15, 25], gesture: [15, 25],
    emotion: [15, 25], dance: [15, 25], walk: [15, 25]
  };

  /**
   * 统一规则核心：在指定场景分类的在盘动作里随机挑一个播放。
   * @param scene  分类名（idle/gesture/emotion/walk/dance/pose）
   * @param preferEmotion  优先匹配的情绪（缺省取当前情绪）
   * @param opts  { hold?: number[秒区间], anyEmotion?: bool }
   * @returns 播放的动作名；该分类没有可播动作时返回 null
   */
  App.pickLibraryActionByScene = function pickLibraryActionByScene(scene, preferEmotion, opts) {
    const config = App._animLibraryConfig;
    if (!config || !config.categories || !config.categories[scene]) return null;
    if (App._mixamoActiveClip) return null; // 已有动作在播，不打断

    const cat = config.categories[scene];
    // 只播“已加载 + 当前角色允许”的在盘动作
    let available = cat.animations.filter(a => App.mixamoClips[a.name] && App.isAnimAllowed(a.name));
    if (!available.length) return null;

    const emotion = preferEmotion || App.emotionSource || 'neutral';
    let pool = available.filter(a => a.emotion === emotion);
    // 尽量随机：情绪匹配池存在时仍有 40% 概率放宽到全分类随机，避免总播同一套应景动作
    if (!pool.length || (opts && opts.anyEmotion) || Math.random() < 0.4) pool = available;
    // 避免与上一次连续重复
    if (pool.length > 1 && App._lastScheduledClip) {
      const noRepeat = pool.filter(a => a.name !== App._lastScheduledClip);
      if (noRepeat.length) pool = noRepeat;
    }
    const anim = pool[Math.floor(Math.random() * pool.length)];

    // 循环动作限时持有（到点释放回程序式动作）；单次动作播完自动释放
    const hold = (opts && opts.hold) || App.LIBRARY_SCENE_HOLD[scene] || [4, 7];
    const holdMs = (hold[0] + Math.random() * (hold[1] - hold[0])) * 1000;
    if (anim.loop) {
      setTimeout(() => {
        if (App._mixamoActiveClip === anim.name) App.stopMixamoClip();
      }, holdMs);
    }

    App.playLibraryClip(anim.name, { loop: anim.loop });
    App._lastScheduledClip = anim.name;
    App.nextActionTimer = 0; // 不摇骰子：动作播完直接硬切下一个随机动作
    console.log('[AnimLib] 统一调度: [' + scene + '] ' + anim.name +
      ' (动作情绪=' + anim.emotion + ', 当前情绪=' + emotion + ', 限时=' + (holdMs / 1000).toFixed(1) + 's)');
    return anim.name;
  };

  /**
   * 统一调度入口：完全随机——从所有已加载且角色允许的动作里等概率挑一个播放，
   * 不按情绪应景、不按分类加权、不防重复。
   * 播放成功时预置好程序式调度的间隔——动作播完（单次 finished / 循环限时到）
   * 释放回调度口后，按常规间隔自然恢复轮换。
   * @param scene  保留参数（兼容调用方），完全随机模式下忽略
   */
  App.tryStartLibraryAction = function tryStartLibraryAction(scene, opts) {
    if (!App._animLibraryLoaded || !App._animLibraryConfig) return null;
    // 完全随机：不按情绪应景、不按分类加权、不防重复——
    // 从所有已加载且角色允许的动作里等概率随机挑一个。
    // 仅排除 walk 分类（FBX 行走带位移轨道，原地播放会滑步，行走由移动系统负责）。
    const all = Object.values(App.mixamoClips).filter(c =>
      App.isAnimAllowed(c.name) && animCategoryOf(c.name) !== 'walk');
    if (!all.length) return null;
    const anim = all[Math.floor(Math.random() * all.length)];
    const cat = animCategoryOf(anim.name);
    // 循环动作限时持有：到点直接硬切下一个随机动作（不留静息空窗）；
    // 单次动作播完由 finished 事件直接接下一个
    const hold = (opts && opts.hold) || App.LIBRARY_SCENE_HOLD[cat] || [4, 7];
    const holdMs = (hold[0] + Math.random() * (hold[1] - hold[0])) * 1000;
    if (anim.loop) {
      setTimeout(() => {
        if (App._mixamoActiveClip === anim.name) {
          // 直接接下一个随机动作（保持一直在动）；库无可播动作才交回程序微动作
          if (!App.tryStartLibraryAction || !App.tryStartLibraryAction()) {
            App.stopMixamoClip();
          }
        }
      }, holdMs);
    }
    App.playLibraryClip(anim.name, { loop: anim.loop });
    App._lastScheduledClip = anim.name;
    App.nextActionTimer = 0; // 不摇骰子：动作播完直接硬切下一个随机动作
    // 动态模式：播放成功后异步预取一个未加载动作，下次切换已就绪
    if (App._prefetchNext) App._prefetchNext();
    console.log('[AnimLib] 完全随机调度: [' + cat + '] ' + anim.name +
      ' (限时=' + (holdMs / 1000).toFixed(1) + 's)');
    return anim.name;
  };

  // ==================== 情绪联动 ====================
  // 情绪变化时自动切换待机动作的逻辑挂在 emotion_controller 扩展中
});
