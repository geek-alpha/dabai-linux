import type { AppKernel } from '../types/app-kernel.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';
export default (function init(App: AppKernel) {
  const {
    THREE: THREE,
    GLTFLoader: GLTFLoader,
    VRMLoaderPlugin: VRMLoaderPlugin,
    VRMUtils: VRMUtils
  } = App;
  // 陀螺仪回调（模块级引用，供低功耗模式移除监听）
  /* ============================================================
   *  Three.js 场景
   * ============================================================ */
  App.scene = undefined;
  App.camera = undefined;
  App.renderer = undefined;
  App.clock = undefined;
  App.starField = null; // 星空粒子 (用于陀螺仪视差)
  App.backgroundGroup = null; // 加载的 3D 背景模型容器
  App._bgCenterX = 0; // 背景模型包围盒中心X
  App._bgCenterZ = 0; // 背景模型包围盒中心Z
  App._groundRaycaster = new THREE.Raycaster(); // 地面射线检测
  App._groundRayOrigin = new THREE.Vector3();
  App._groundRayDir = new THREE.Vector3(0, -1, 0);
  // 角色物理状态
  App._playerVelocityY = 0;    // 垂直速度
  App._playerIsGrounded = true; // 是否在地面
  App._playerGroundY = 0;       // 地面高度
  App._GRAVITY = 4.5;            // 重力加速度 (降低防止高速穿透薄物体)
  App._MAX_FALL_SPEED = 6.0;     // 最大下落速度 (0.05s子步×6=0.3m/帧，不穿透)
  App._PHYSICS_SUBSTEPS = 4;     // 物理子步数 (将dt拆分为更小步长)
  App.backgroundAutoRotate = false; // 背景是否缓慢自转（默认关闭，避免场景莫名移动）
  App.proceduralChar = null; // 已弃用
  App.modelGroup = null; // 加载的模型容器
  App.currentAvatar = null; // 当前显示的角色
  App.parts = {}; // 场景部件
  App.vrm = null; // 当前 VRM 实例
  App.morphTargets = []; // GLTF 口型 morph targets [{mesh, index, name}]
  App.headBone = null; // 模型头部骨骼 (用于动画)
  App.modelType = null; // null | 'procedural' | 'gltf' | 'vrm'
  App.blinkTimer = 0;
  App.nextBlinkAt = 2 + Math.random() * 3;
  App.blinkType = 'normal';
  App.blinkPhase = 0;
  App.blinkDuration = 0.15;
  // 预调度首个眨眼类型
  if (App.scheduleNextBlink) App.scheduleNextBlink();
  App.userRotY = 0;
  App.userRotX = 0;
  App.isDragging = false; // 是否正在拖拽旋转（松手后 dragOrbitYaw/Pitch 自动衰减回 0）
  App.dragOrbitYaw = 0; // 拖拽相机水平环绕偏移
  App.dragOrbitPitch = 0; // 拖拽相机垂直环绕偏移
  App.smoothRotY = 0; // 平滑后的旋转值，防止拖拽时弹簧骨骼穿模
  App.smoothRotX = 0;
  App.smoothWalkFaceOff = 0; // 平滑后的行走朝向偏移，避免瞬间转身
  App.lastUserActivityTime = Date.now(); // 上次用户活动时间，用于 AI 自主触发
  App.lastProactiveTime = 0; // 上次 AI 自主触发时间
  App.PROACTIVE_SILENCE_MS = 60000; // 60 秒无交互才触发 AI 主动说话（不一直废话原则）
  App.PROACTIVE_COOLDOWN_MS = 120000; // 触发后至少冷却 2 分钟
  App._sentAvatarName = null; // 已向服务器发送过的模型名（防重复通知）
  App._sentBgName = null; // 已向服务器发送过的背景名（防重复通知）
  App._isBooting = true; // 启动中，跳过 sendAIAction 等主动触发
  App.lastInteractionTime = Date.now() / 1000; // 用户最后交互时间
  App.AUTO_CAM_DELAY = 15; // 无操作X秒后触发一次心有灵犀
  App.MUTUAL_GAZE_WINDOW = 4; // 心有灵犀持续窗口（秒），只触发一次而非一直持续
  App.recordInteraction = function recordInteraction() {
    App.lastInteractionTime = Date.now() / 1000;
    App.lastUserActivityTime = Date.now();
  };
  App.lerp = (cur, tgt, k) => cur + (tgt - cur) * k;
  App.autoLookTarget = null; // 自动跟踪时的平滑 lookAt 目标（首帧懒初始化）
  App.mutualGaze = false; // 心有灵犀：15秒无操作后短暂触发
  App.wasMutualGaze = false; // 上一帧的心有灵犀状态，用于检测激活边沿
  App.gazeHeadTiltAcc = 0; // 心有灵犀歪头角度（需跨帧累积）
  App.gazeBoostUntil = 0; // 心有灵犀增益窗口结束时间（交互后短暂触发凝视）
  App.camZoom = 1.0; // 缩放倍率 (0.05=极限拉近, 12.0=极限拉远)
  App.MIN_ZOOM = 0.05;
  App.MAX_ZOOM = 12.0;
  /* ---------- 隐私护栏（非管理员）：收紧上下环绕角度与最近距离 ----------
   * 普通用户不许贴到角色身上，也不许从下往上打量（穿模 / 走光）。
   * 管理员全部走原始值，行为不变。 */
  App.IS_ADMIN = window.__ROLE === 'admin';
  App.USER_ORBIT_PITCH_LIMIT = Math.PI / 3; // 上下环绕角度上限（弧度，60°）
  App.USER_MIN_CAM_DISTANCE = 1.0; // 相机基准距离下限（米）
  App.USER_MIN_CAM_HORIZ = 1.0; // 相机与角色的水平距离硬下限（米，再近就穿模）
  App.USER_MIN_CAM_HEIGHT = 1.8; // 相机高度下限（米，防贴地仰视）
  App.USER_MIN_VIEW_HEIGHT = 1.0; // 相机高度绝对下限（米）
  App.USER_FPV_MIN_HEIGHT = 1.2; // 第一人称视点最低高度（米）
  App.USER_FPV_MIN_DISTANCE = 1.0; // 第一人称与角色的最小水平距离（米）
  // 缩放下限与基准距离联动：距离滑块调到多小，镜头都恰好推到 USER_MIN_CAM_HORIZ 为止
  App.userMinZoom = () => (App.IS_ADMIN ? App.MIN_ZOOM : Math.max(App.MIN_ZOOM, Math.min(1, App.USER_MIN_CAM_HORIZ / Math.max(App.cameraDistance, 0.1))));
  App.userMinCamDistance = () => (App.IS_ADMIN ? 0.1 : App.USER_MIN_CAM_DISTANCE);
  App.userMinCamHeight = () => (App.IS_ADMIN ? 0.1 : App.USER_MIN_CAM_HEIGHT);
  // 每帧兜底：拖拽/缩放之外还有 FPV 偏移、聚焦退出偏移、脏存档三条路径能改相机，
  // 光靠输入处夹紧拦不住，所以最终目标位置再过一道。
  App.clampCameraForUser = function clampCameraForUser(p) {
    const cx = App.autoLookTarget ? App.autoLookTarget.x : 0;
    const cz = App.autoLookTarget ? App.autoLookTarget.z : 0;
    const dx = p.x - cx;
    const dz = p.z - cz;
    const horiz = Math.hypot(dx, dz);
    const minHoriz = App.USER_MIN_CAM_HORIZ;
    if (horiz < minHoriz) {
      if (horiz > 1e-4) {
        const k = minHoriz / horiz;
        p.x = cx + dx * k;
        p.z = cz + dz * k;
      } else {
        p.z = cz + minHoriz;
      }
    }
    if (p.y < App.USER_MIN_VIEW_HEIGHT) p.y = App.USER_MIN_VIEW_HEIGHT;
  };
  App.PINCH_SENSITIVITY = 1.8; // 双指捏合缩放灵敏度，>1 响应更快
  // FPV 退出后保留的相机偏移（让探索时调整的视角在退出后不丢失）
  App.camOffsetX = 0;
  App.camOffsetY = 0;
  App.camOffsetZ = 0;
  App.pinching = false; // 双指捏合中 (禁用拖拽旋转)
  App.gyroYaw = 0; // 平滑后的陀螺仪旋转偏移（普通模式轨道视角基准）
  // 相机高度与倾斜设置
  App.gyroPitch = 0;
  App.vrShake = null; // VR模式摇晃检测状态（开启时初始化：上下/左右强度0-100）
  App.cameraHeight = 2.55;
  App.cameraTiltDeg = 9;
  App.cameraDistance = 2.5;
  App.CAM_SETTINGS_KEY = 'dabai.cameraSettings';
  App.targetCamPos = new THREE.Vector3(0, App.cameraHeight, App.cameraDistance);
  App.DEFAULT_CAM_POS = new THREE.Vector3(0, 2.55, 2.5); // 移动模式：选中角色或背景并拖拽平移
  App.moveMode = false; // 是否处于移动模式
  App.selectedTarget = null; // 当前选中的可移动对象 (modelGroup / backgroundGroup)
  App.selectionHelper = null; // BoxHelper 选中高亮
  App.raycaster = new THREE.Raycaster();
  App.pointerNdc = new THREE.Vector2();
  App.dragPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0); // 水平面，dynamically 调整
  App.dragHitPoint = new THREE.Vector3();
  App.dragOffsetX = 0; // 拖拽起点相对于目标中心的偏移
  // 点击角色部位交互
  App.dragOffsetZ = 0;
  App.clickWobble = {
    active: false,
    posX: 0, posZ: 0, posY: 0,
    velX: 0, velZ: 0, velY: 0,
    rotZ: 0, rotX: 0, rotY: 0,   // 身体摇晃角度（Z=左右倾，X=前后仰，Y=扭转）
    rotVelZ: 0, rotVelX: 0, rotVelY: 0,
    // 当前偏移（撤销用）
    prevPosX: 0, prevPosZ: 0, prevPosY: 0,
    prevRotZ: 0, prevRotX: 0, prevRotY: 0,
    stiffness: 0.01,            // 弹簧刚度（越小回正越慢）
    damping: 0.985,             // 速度衰减（越接近1晃动越持久）
    rotStiffness: 0.008,
    rotDamping: 0.985,
    settleThreshold: 0.0003
  };
  App.focusPart = {
    active: false,
    target: new THREE.Vector3(),
    lookAt: new THREE.Vector3(),
    time: 0,
    name: ''
  };
  App.clickStartPos = {
    x: 0,
    y: 0
  };
  App.clickRaycaster = new THREE.Raycaster();
  App.dragTotalRot = 0; // 累计拖拽旋转量
  App.zoomNet = 0; // 累计净缩放（正=拉远，负=推近）
  App.zoomAbsTotal = 0; // 累计绝对缩放量
  App.zoomDebounceTimer = null; // 缩放防抖
  App.DRAG_THRESHOLD = 0.5; // 触发AI反应的拖拽旋转阈值（rad）
  App.ZOOM_THRESHOLD = 0.2; // 触发AI反应的缩放阈值
  // ==================== 3D 角色统一动作系统 ====================
  // 动作大类：摆姿势 / 走路 / 转圈，三者等概率轮换，作为角色属性存在
  App.ActionType = {
    POSE: 'pose',
    WALK: 'walk',
    TURN: 'turn',
    DANCE: 'dance'
  };
  App.currentAction = null; // 当前执行的动作对象 { type, ... }
  App.nextActionTimer = 0; // 下一次动作触发倒计时
  App.ACTION_GAP_MIN = 0; // 动作间最短间隔（秒）——0：播完立即接下一个，不摇骰子
  App.ACTION_GAP_MAX = 0; // 动作间最长间隔（秒）——0：播完立即接下一个，不摇骰子
  App.currentPose = 'rest'; // 当前姿态名
  App.prevPose = 'rest'; // 前一个姿态名（用于混合期间保留旧姿态的收尾动画）
  App.poseBlend = 0; // 姿态混合因子：0=完全rest, 1=完全当前姿态
  App.poseBlendTarget = 0; // 混合目标值
  App.poseTimer = 0; // 姿态定时器（多功能：进入耗时/保持耗时/退出耗时）
  App.posePhase = 'idle'; // 'idle' | 'entering' | 'holding' | 'exiting' → 'idle'
  App.POSE_ENTER_TIME = 1.6; // 进入姿态耗时（秒）——更舒展，衔接更柔和
  App.POSE_EXIT_TIME = 1.2; // 退出姿态耗时（秒）——自然收尾
  App.POSE_HOLD_MIN = 4.0; // 姿态最短保持（秒）
  App.POSE_HOLD_MAX = 9.0; // 姿态最长保持（秒）
  App.POSE_GAP_MIN = 3.0; // 姿态间最短间隔（秒）
  App.POSE_GAP_MAX = 7.0; // 姿态间最长间隔（秒）
  // 转圈动作状态
  App.turnProgress = 0; // 0~1
  App.turnStartAngle = 0;
  App.turnTargetAngle = 0;
  App.turnDuration = 0;
  App.turnElapsed = 0;
  // 跳舞动作状态
  App.danceElapsed = 0;
  App.danceDuration = 0;
  // 随机移动系统（路径化，让角色移动更久、更自然）
  App.idleWalkTarget = null;
  App.idleWalkStart = null;
  App.idleWalkProgress = 0;
  App.idleWalkSpeed = 0.3;
  App.walkFacingAngle = 0;
  App.walkPath = []; // 路径点队列 {x,z}
  App.walkSegmentIndex = 0; // 当前路径段
  App.walkSegmentsTotal = 1; // 总段数
  App._smoothTeleport = null; // 平滑过渡（复位/恢复位置时替代硬瞬移）
  App.idleEnergy = 0; // idle 活力周期，用于控制身体摆动幅度
  App.WALK_RANGE = 0.11; // 最大行走距离，降低后一次完整移动总时长不超过2秒
  App.WALK_PATH_MAX_SEGMENTS = 4; // 一次移动最多连续走几段（当前动作系统每段即一次 walk）
  App.WALK_SPEED = 0.225; // 行走线速度（单位/秒），步频降低一半
  App.WALK_STEP_LENGTH = 0.35; // 每步长度（单位），小步
  // AI 自主行走（大厅）：自然步行速度（约 1.6 单位/秒、步长 0.8）。
  // 不再沿用游戏内移动速度 3.5 —— 游戏是宽旷地图的操控手感，大厅是近距离观赏场景，
  // 3.5 单位/秒在橱窗式小场景里显得像小跑，改为自然的步行节奏
  App.AI_LOBBY_WALK_SPEED = 1.6; // 大厅 AI 行走线速度（单位/秒），自然步行速度
  App.AI_LOBBY_WALK_STEP_LENGTH = 0.8; // 大厅 AI 步长（单位），配合步速约 2 步/秒
  // 姿态定义：boneName -> {x, y, z} —— 在 rest pose 基础上的偏移量（叠加在 ARM_REST_Z 等默认值之上）
  App.POSES = {
    rest: {},
    // 双手向上伸懒腰，全身舒展
    full_stretch: {
      leftUpperArm: {
        z: -0.15,
        x: -0.12
      },
      leftLowerArm: {
        x: 0.10
      },
      rightUpperArm: {
        z: 0.15,
        x: -0.12
      },
      rightLowerArm: {
        x: 0.15
      },
      leftHand: {
        x: -0.25
      },
      rightHand: {
        x: -0.25
      },
      spine: {
        x: -0.05
      },
      chest: {
        x: -0.02
      },
      head: {
        x: -0.12
      }
    },
    // 身体右转、头向右上方看（好奇张望）
    look_around_right: {
      spine: {
        y: 0.08
      },
      head: {
        y: 0.25,
        x: -0.05
      }
    },
    // 身体左转、头向左看
    look_around_left: {
      spine: {
        y: -0.08
      },
      head: {
        y: -0.25,
        x: -0.05
      }
    },
    // 右手摸胸口（被感动/被戳中）—— 右手微曲掌心向内
    hand_on_chest: {
      rightUpperArm: {
        z: -0.05,
        x: -0.22
      },
      rightLowerArm: {
        x: -0.45
      },
      rightHand: {
        y: 0.10,
        z: -0.06
      },
      leftUpperArm: {
        z: 0.01,
        x: 0.02
      },
      leftHand: {
        y: -0.08
      },
      head: {
        x: -0.05,
        y: 0.03
      }
    },
    // 双手交叉抱胸 —— 手掌自然放松内扣
    cross_arms: {
      leftUpperArm: {
        z: 0.10,
        x: 0.15
      },
      leftLowerArm: {
        x: -0.40
      },
      leftHand: {
        z: 0.12,
        x: -0.15
      },
      rightUpperArm: {
        z: -0.10,
        x: -0.20
      },
      rightLowerArm: {
        x: -0.40
      },
      rightHand: {
        z: -0.12,
        x: -0.15
      }
    },
    // 兴奋举手 —— 五指张开
    excited: {
      leftUpperArm: {
        z: -0.40,
        x: -0.15
      },
      leftLowerArm: {
        x: -0.15
      },
      leftHand: {
        x: -0.20,
        y: -0.10
      },
      rightUpperArm: {
        z: 0.40,
        x: -0.15
      },
      rightLowerArm: {
        x: -0.10
      },
      rightHand: {
        x: -0.20,
        y: 0.10
      },
      spine: {
        x: -0.03
      },
      head: {
        x: -0.08
      }
    },
    // 右手扶下巴思考 —— 手指微卷托腮
    thinking_hand: {
      rightUpperArm: {
        z: -0.10,
        x: -0.18
      },
      rightLowerArm: {
        x: -0.45
      },
      rightHand: {
        y: 0.10,
        z: 0.05
      },
      leftUpperArm: {
        z: -0.02,
        x: 0.05
      },
      leftHand: {
        z: -0.05
      },
      head: {
        x: -0.06,
        y: 0.04
      }
    },
    // 害羞：低头、双手在身前 —— 双手交叠
    shy_demure: {
      leftUpperArm: {
        z: -0.08,
        x: 0.15
      },
      leftLowerArm: {
        x: -0.20
      },
      leftHand: {
        z: 0.08,
        x: -0.10
      },
      rightUpperArm: {
        z: 0.08,
        x: -0.15
      },
      rightLowerArm: {
        x: -0.20
      },
      rightHand: {
        z: -0.08,
        x: -0.10
      },
      head: {
        x: 0.10
      }
    },
    // 微微侧头（卖萌/疑惑）
    head_tilt: {
      head: {
        z: 0.10,
        x: -0.03
      },
      neck: {
        z: 0.04
      },
      spine: {
        z: 0.01
      }
    },
    // 单手叉腰 —— 叉腰手微张，另一只手自然下垂
    hand_on_hip: {
      leftUpperArm: {
        z: -0.05,
        x: 0.40
      },
      leftLowerArm: {
        x: -1.00
      },
      leftHand: {
        y: -0.15,
        x: 0.10
      },
      rightUpperArm: {
        z: 0.03,
        x: -0.05
      },
      rightHand: {
        z: 0.05
      },
      spine: {
        x: 0.01
      }
    },
    // 摇摆身体 —— 身体重心在左右脚之间转移
    dance_sway: {
      leftUpperArm: {
        z: -0.10,
        x: 0.08
      },
      leftHand: {
        z: 0.10
      },
      rightUpperArm: {
        z: 0.10,
        x: -0.08
      },
      rightHand: {
        z: -0.10
      },
      head: {
        z: 0.04
      },
      spine: {
        y: 0.02
      }
    },
    // 打哈欠 —— 抬手遮嘴，全身舒展
    yawn_stretch: {
      rightUpperArm: {
        z: -0.20,
        x: -0.35
      },
      rightLowerArm: {
        x: -0.60
      },
      rightHand: {
        y: 0.10,
        x: -0.10
      },
      leftUpperArm: {
        z: -0.45,
        x: 0.05
      },
      leftLowerArm: {
        x: -0.30
      },
      leftHand: {
        x: -0.20
      },
      spine: {
        x: -0.04
      },
      head: {
        x: -0.08
      },
      chest: {
        x: -0.02
      }
    },
    // 揉眼睛 —— 双手揉眼睛/擦眼泪
    rub_eyes: {
      leftUpperArm: {
        z: -0.30,
        x: 0.25
      },
      leftLowerArm: {
        x: -0.80
      },
      leftHand: {
        y: -0.10,
        x: -0.15
      },
      rightUpperArm: {
        z: 0.30,
        x: -0.18
      },
      rightLowerArm: {
        x: -0.55
      },
      rightHand: {
        y: 0.10,
        x: -0.15
      },
      head: {
        x: -0.01
      }
    },
    // 转半身回头看（左右各一次作为子姿态）
    glance_back: {
      spine: {
        y: 0.35
      },
      head: {
        y: 0.20
      },
      leftUpperArm: {
        z: -0.02,
        x: 0.05
      },
      rightUpperArm: {
        z: 0.02,
        x: -0.05
      }
    },
    // ======= 舞蹈/舒展姿态 =======
    // 扭腰右转 —— 脊柱+髋部右转，头回看
    waist_twist_right: {
      spine: {
        y: 0.25
      },
      hips: {
        y: 0.15
      },
      head: {
        y: 0.18,
        x: -0.03
      },
      rightUpperArm: {
        z: 0.15,
        x: -0.15
      },
      leftUpperArm: {
        z: -0.08,
        x: 0.05
      }
    },
    // 扭腰左转
    waist_twist_left: {
      spine: {
        y: -0.25
      },
      hips: {
        y: -0.15
      },
      head: {
        y: -0.18,
        x: -0.03
      },
      rightUpperArm: {
        z: 0.08,
        x: -0.05
      },
      leftUpperArm: {
        z: -0.15,
        x: 0.15
      }
    },
    // 大开双臂 —— 双手向两侧舒展打开，身体微后仰
    arms_wide: {
      leftUpperArm: {
        z: -0.55,
        x: 0.15
      },
      leftLowerArm: {
        x: -0.35
      },
      leftHand: {
        x: -0.15,
        z: 0.05
      },
      rightUpperArm: {
        z: 0.55,
        x: -0.15
      },
      rightLowerArm: {
        x: -0.35
      },
      rightHand: {
        x: -0.15,
        z: -0.05
      },
      spine: {
        x: -0.04
      },
      chest: {
        x: -0.02
      },
      head: {
        x: -0.06
      }
    },
    // 头右摆撒娇 —— 头向右歪，眼含笑意
    head_sway_right: {
      head: {
        z: 0.15
      },
      neck: {
        z: 0.06
      },
      spine: {
        z: 0.02
      },
      leftUpperArm: {
        z: -0.05,
        x: 0.08
      },
      rightUpperArm: {
        z: 0.03,
        x: -0.05
      }
    },
    // 头左摆撒娇
    head_sway_left: {
      head: {
        z: -0.15
      },
      neck: {
        z: -0.06
      },
      spine: {
        z: -0.02
      },
      leftUpperArm: {
        z: -0.03,
        x: 0.05
      },
      rightUpperArm: {
        z: 0.05,
        x: -0.08
      }
    },
    // 右顶胯 —— 髋部右顶+身体S曲线
    hip_sway_right: {
      hips: {
        z: 0.06,
        y: 0.06
      },
      spine: {
        z: -0.04,
        y: 0.05
      },
      head: {
        z: -0.04
      },
      leftUpperArm: {
        z: -0.05,
        x: 0.05
      },
      rightUpperArm: {
        z: 0.08,
        x: 0.10
      },
      rightHand: {
        z: -0.05
      }
    },
    // 左顶胯
    hip_sway_left: {
      hips: {
        z: -0.06,
        y: -0.06
      },
      spine: {
        z: 0.04,
        y: -0.05
      },
      head: {
        z: 0.04
      },
      leftUpperArm: {
        z: -0.08,
        x: -0.10
      },
      leftHand: {
        z: 0.05
      },
      rightUpperArm: {
        z: 0.05,
        x: -0.05
      }
    },
    // 单手高举 —— 右手向上伸直舒展
    one_arm_up: {
      rightUpperArm: {
        z: 0.55,
        x: -0.20
      },
      rightLowerArm: {
        x: -0.15
      },
      rightHand: {
        x: -0.20,
        y: 0.05
      },
      leftUpperArm: {
        z: -0.05,
        x: 0.08
      },
      leftHand: {
        z: 0.05
      },
      spine: {
        x: -0.03,
        z: 0.02
      },
      head: {
        x: -0.08
      },
      chest: {
        x: -0.01
      }
    },
    // 双手交替伸展 —— 一高一低舞蹈式流线
    arms_flow: {
      leftUpperArm: {
        z: -0.50,
        x: 0.25
      },
      leftLowerArm: {
        x: -0.40
      },
      leftHand: {
        x: -0.15,
        z: 0.08
      },
      rightUpperArm: {
        z: 0.20,
        x: -0.30
      },
      rightLowerArm: {
        x: -0.20
      },
      rightHand: {
        y: 0.05
      },
      spine: {
        y: 0.04,
        z: 0.02
      },
      head: {
        y: 0.05,
        x: -0.04
      }
    },
    // 身体波浪 —— 脊柱S弯+双手随动，像舞蹈波纹
    body_wave: {
      spine: {
        y: 0.08,
        z: 0.04,
        x: -0.02
      },
      chest: {
        x: -0.02
      },
      hips: {
        y: -0.04,
        z: -0.02
      },
      head: {
        z: -0.06,
        x: -0.05
      },
      leftUpperArm: {
        z: -0.20,
        x: 0.12
      },
      leftLowerArm: {
        x: -0.25
      },
      leftHand: {
        z: 0.08
      },
      rightUpperArm: {
        z: 0.25,
        x: -0.08
      },
      rightLowerArm: {
        x: -0.30
      },
      rightHand: {
        z: -0.06
      }
    },
    // 回眸一笑 —— 身体微侧+头回转+右手轻抬
    glance_smile: {
      spine: {
        y: 0.18
      },
      head: {
        y: 0.35,
        x: -0.06,
        z: 0.04
      },
      neck: {
        y: 0.05
      },
      rightUpperArm: {
        z: 0.10,
        x: -0.20
      },
      rightHand: {
        y: 0.10,
        z: -0.05
      },
      leftUpperArm: {
        z: -0.05,
        x: 0.05
      }
    }
  }; // 对话姿态集 —— 每个对话状态切换时随机选用，增强氛围感
  App.CONV_POSES = {
    // SPEAKING 说话姿态 —— 带手势的生动表达
    spk_open: {
      leftUpperArm: {
        z: -0.25,
        x: 0.15
      },
      rightUpperArm: {
        z: 0.25,
        x: -0.15
      },
      leftLowerArm: {
        x: -0.30
      },
      rightLowerArm: {
        x: -0.30
      },
      leftHand: {
        x: -0.10
      },
      rightHand: {
        x: -0.10
      }
    },
    spk_point: {
      rightUpperArm: {
        z: -0.15,
        x: -0.20
      },
      rightLowerArm: {
        x: -0.30
      },
      rightHand: {
        y: 0.10
      },
      leftUpperArm: {
        z: -0.02,
        x: 0.05
      }
    },
    spk_passion: {
      leftUpperArm: {
        z: -0.40,
        x: 0.20
      },
      leftLowerArm: {
        x: -0.20
      },
      rightUpperArm: {
        z: 0.40,
        x: -0.20
      },
      rightLowerArm: {
        x: -0.20
      },
      leftHand: {
        x: -0.15
      },
      rightHand: {
        x: -0.15
      },
      head: {
        x: -0.05
      }
    },
    spk_explain: {
      leftUpperArm: {
        z: -0.15,
        x: 0.10
      },
      rightUpperArm: {
        z: 0.20,
        x: -0.25
      },
      rightLowerArm: {
        x: -0.35
      },
      rightHand: {
        y: 0.05,
        z: 0.05
      }
    },
    // LISTENING 倾听姿态 —— 关注/好奇
    lstn_tilt: {
      head: {
        z: 0.08,
        x: -0.02
      },
      neck: {
        z: 0.03
      },
      leftUpperArm: {
        z: 0.02,
        x: 0.03
      },
      rightUpperArm: {
        z: -0.02,
        x: -0.03
      }
    },
    lstn_forward: {
      spine: {
        x: 0.02
      },
      head: {
        x: -0.04
      },
      leftUpperArm: {
        z: -0.03,
        x: 0.05
      },
      rightUpperArm: {
        z: 0.03,
        x: -0.05
      }
    },
    lstn_folded: {
      leftUpperArm: {
        z: -0.05,
        x: 0.12
      },
      leftLowerArm: {
        x: -0.25
      },
      rightUpperArm: {
        z: 0.05,
        x: -0.12
      },
      rightLowerArm: {
        x: -0.25
      },
      leftHand: {
        z: 0.05
      },
      rightHand: {
        z: -0.05
      }
    },
    // THINKING 思考姿态 —— 沉思/计算中
    thk_lookup: {
      head: {
        x: -0.10
      },
      spine: {
        x: -0.02
      },
      rightUpperArm: {
        z: 0.02,
        x: -0.05
      }
    },
    thk_chin: {
      rightUpperArm: {
        z: -0.10,
        x: -0.18
      },
      rightLowerArm: {
        x: -0.40
      },
      rightHand: {
        y: 0.08,
        z: 0.05
      },
      leftUpperArm: {
        z: 0.05,
        x: 0.10
      },
      leftLowerArm: {
        x: -0.50
      },
      head: {
        x: -0.04,
        y: 0.03
      }
    },
    thk_armsfold: {
      leftUpperArm: {
        z: 0.05,
        x: 0.12
      },
      leftLowerArm: {
        x: -0.35
      },
      rightUpperArm: {
        z: -0.05,
        x: -0.15
      },
      rightLowerArm: {
        x: -0.35
      },
      leftHand: {
        z: 0.08
      },
      rightHand: {
        z: -0.08
      },
      head: {
        x: -0.03
      }
    }
  }; // 统一姿态库：对话姿态与 idle 姿态合并，作为动作大类的 POSE 候选
  App.ALL_POSES = Object.assign({}, App.POSES, App.CONV_POSES);
  App.pickRandomPose = function pickRandomPose() {
    const names = Object.keys(App.ALL_POSES).filter(n => n !== 'rest' && n !== App.currentPose && n !== App.prevPose);
    if (names.length === 0) return Object.keys(App.ALL_POSES).filter(n => n !== 'rest')[0] || 'rest';
    return names[Math.floor(Math.random() * names.length)];
  }; // 姿态生命周期管理：作为动作大类 POSE 的子状态机
  App.updatePoseTimer = function updatePoseTimer(dt) {
    // 非 POSE 动作时，让当前姿态平滑退回 rest
    if (!App.currentAction || App.currentAction.type !== App.ActionType.POSE) {
      if (App.currentPose !== 'rest' && App.posePhase !== 'exiting') {
        App.prevPose = App.currentPose;
        App.currentPose = 'rest';
        App.poseBlendTarget = 0;
        App.poseTimer = App.POSE_EXIT_TIME;
        App.posePhase = 'exiting';
      }
      return;
    }
    switch (App.posePhase) {
      case 'idle':
        if (App.poseTimer > 0) {
          App.poseTimer -= dt;
          return;
        }
        if (App.currentPose !== 'rest') {
          App.poseBlendTarget = 0;
          App.poseTimer = App.POSE_EXIT_TIME;
          App.posePhase = 'exiting';
        } else {
          App.prevPose = App.currentPose;
          App.currentPose = App.pickRandomPose();
          App.poseBlendTarget = 1;
          App.poseTimer = App.POSE_ENTER_TIME + Math.random() * 0.5;
          App.posePhase = 'entering';
        }
        break;
      case 'entering':
        App.poseTimer -= dt;
        if (App.poseTimer <= 0) {
          App.poseBlend = 1;
          App.poseTimer = App.POSE_HOLD_MIN + Math.random() * (App.POSE_HOLD_MAX - App.POSE_HOLD_MIN);
          App.posePhase = 'holding';
        }
        break;
      case 'holding':
        App.poseTimer -= dt;
        if (App.poseTimer <= 0) {
          App.poseBlendTarget = 0;
          App.poseTimer = App.POSE_EXIT_TIME + Math.random() * 0.3;
          App.posePhase = 'exiting';
        }
        break;
      case 'exiting':
        App.poseTimer -= dt;
        if (App.poseTimer <= 0) {
          App.prevPose = App.currentPose;
          App.currentPose = 'rest';
          App.poseBlend = 0;
          App.poseBlendTarget = 0;
          App.posePhase = 'idle';
          App.currentAction = null;
          App.nextActionTimer = App.ACTION_GAP_MIN + Math.random() * (App.ACTION_GAP_MAX - App.ACTION_GAP_MIN);
        }
        break;
    }
  }; // ==================== 统一全身走路动画（游戏模式 + 非游戏模式共用） ====================
  //
  // 本函数是唯一的走路动画实现。游戏模式、非游戏 AI 移动、非游戏 idle walk 均调用此函数。
  // moveVec 格式: { x: world_dx, z: world_dz, isMoving: bool }
  //
  App.applyFullBodyWalkAnimation = function applyFullBodyWalkAnimation(dt, moveVec) {
    const avatar = App.currentAvatar;
    if (!avatar) return;

    const isMoving = moveVec && moveVec.isMoving;
    const B = App.vrmBones;
    const parts = App.parts;
    // 步长：游戏模式 ~3.0，非游戏 ~0.35，可由调用方通过 moveVec.stepLength 覆盖
    const stepLen = (moveVec && moveVec.stepLength) || (App.WALK_STEP_LENGTH || 0.35);

    // 摆臂幅度：0.35（≈20°）—— 自然优雅；原 0.80 乘速度因子后可达 ±1.6rad（92°），
    // 在 z=-1.35 下垂基准上大幅甩动会扭曲变形
    const ARM_SWING_AMP = 0.35;
    const LEG_SWING_AMP = 0.80;
    const FOOT_LIFT_AMP = 0.40;
    const BOB_AMP = 0.10;
    // 腿/脚用较快的插值（原 0.08 对高频正弦目标衰减过大 → 摆腿只剩几度，腿僵硬不动）
    const LEG_LERP = 0.45;
    const FOOT_LERP = 0.45;

    // ramp
    if (isMoving) {
      App._fullWalkRampT = (App._fullWalkRampT || 0) + dt;
    } else {
      App._fullWalkRampT = 0;
    }
    const rampFactor = 0.35 + 0.65 * Math.min(1.0, (App._fullWalkRampT || 0) / 1.2);
    App._fullWalkRampFactor = rampFactor;

    // phase
    let actualSpeed = 0;
    if (isMoving && dt > 0.0001) {
      actualSpeed = Math.sqrt((moveVec.x || 0) ** 2 + (moveVec.z || 0) ** 2) / dt;
      App._fullWalkPhase = (App._fullWalkPhase || 0) + dt * (actualSpeed / stepLen) * Math.PI * 2 * rampFactor;

      if (!App._fullWalkAnimActive) {
        App._fullWalkAnimActive = true;
        App._fullWalkPhase = 0;
        if (!avatar.userData._baseY) avatar.userData._baseY = avatar.position.y;
      }
      App._fullWalkPhase = App._fullWalkPhase % (Math.PI * 2);
    } else {
      if (App._fullWalkAnimActive) {
        App._fullWalkPhase = (App._fullWalkPhase || 0) + dt * 3;
        if (App._fullWalkPhase > Math.PI * 2) {
          App._fullWalkAnimActive = false;
          App._fullWalkPhase = 0;
          resetFullWalkArms();
          // 大厅由地面系统管理 Y，避免用陈旧 _baseY 拽回旧高度（垂直瞬移）
          if (avatar.userData._baseY !== undefined && false) {
            avatar.position.y = avatar.userData._baseY;
          }
        }
      } else {
        return;
      }
    }

    const phase = App._fullWalkPhase;
    const walkActive = App._fullWalkAnimActive && isMoving;
    const baseSpeed = 3.0;
    const speedFactor = walkActive ? Math.min(2.0, Math.max(0.5, actualSpeed / baseSpeed)) : 1.0;
    const sf = speedFactor * rampFactor;

    // 脚步音效：全局接入点（大厅漫步 / AI 驱动 / 游戏模式移动统一经过这里）
    if (walkActive) {
      if (App.updateFootstepSFX) App.updateFootstepSFX(phase, speedFactor);
    }

    // bob：Y 由地面物理系统（updatePlayerPhysics + 07 行走进地）统一接管，
    // 若此处再用陈旧的 _baseY 写 Y，会在越障/地面检测翻转时把角色拽回旧高度
    // → 垂直"瞬移"。因此行走只贡献摆臂/腿部动画，不写 Y。

    // arms
    const leftArmSwing = walkActive ? -Math.sin(phase) * ARM_SWING_AMP * sf : 0;
    const rightArmSwing = walkActive ? Math.sin(phase) * ARM_SWING_AMP * sf : 0;

    if (B) {
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z || 0, App.armZ(0), 0.07);
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x || 0, leftArmSwing, 0.07);
      }
      if (B.leftLowerArm)
        B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x || 0, -0.15, 0.07);
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z || 0, -App.armZ(0), 0.07);
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x || 0, rightArmSwing, 0.07);
      }
      if (B.rightLowerArm)
        B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x || 0, -0.15, 0.07);

      // legs
      if (B.leftUpperLeg)
        B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x || 0, walkActive ? Math.sin(phase) * LEG_SWING_AMP * sf : 0, LEG_LERP);
      if (B.leftLowerLeg)
        B.leftLowerLeg.rotation.x = App.lerp(B.leftLowerLeg.rotation.x || 0, walkActive ? Math.max(0, Math.sin(phase - 0.4)) * 0.22 * sf : 0, LEG_LERP);
      if (B.leftFoot)
        B.leftFoot.rotation.x = App.lerp(B.leftFoot.rotation.x || 0, walkActive ? Math.max(0, Math.sin(phase)) * FOOT_LIFT_AMP * sf : 0, FOOT_LERP);
      if (B.rightUpperLeg)
        B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x || 0, walkActive ? Math.sin(phase + Math.PI) * LEG_SWING_AMP * sf : 0, LEG_LERP);
      if (B.rightLowerLeg)
        B.rightLowerLeg.rotation.x = App.lerp(B.rightLowerLeg.rotation.x || 0, walkActive ? Math.max(0, Math.sin(phase + Math.PI - 0.4)) * 0.22 * sf : 0, LEG_LERP);
      if (B.rightFoot)
        B.rightFoot.rotation.x = App.lerp(B.rightFoot.rotation.x || 0, walkActive ? Math.max(0, Math.sin(phase + Math.PI)) * FOOT_LIFT_AMP * sf : 0, FOOT_LERP);

      if (B.hips)
        B.hips.rotation.y = App.lerp(B.hips.rotation.y || 0, walkActive ? Math.sin(phase) * 0.03 * sf : 0, 0.06);
    }
    if (parts) {
      if (parts.armL) { parts.armL.rotation.z = 0.25; parts.armL.rotation.x = App.lerp(parts.armL.rotation.x || 0, leftArmSwing, 0.12); }
      if (parts.armR) { parts.armR.rotation.z = -0.25; parts.armR.rotation.x = App.lerp(parts.armR.rotation.x || 0, rightArmSwing, 0.12); }
    }

    function resetFullWalkArms() {
      if (B) {
        if (B.leftUpperArm)  { B.leftUpperArm.rotation.z = App.armZ(0); B.leftUpperArm.rotation.x = 0; }
        if (B.leftLowerArm)  B.leftLowerArm.rotation.x = -0.15;
        if (B.rightUpperArm) { B.rightUpperArm.rotation.z = -App.armZ(0); B.rightUpperArm.rotation.x = 0; }
        if (B.rightLowerArm) B.rightLowerArm.rotation.x = -0.15;
        if (B.leftUpperLeg)  B.leftUpperLeg.rotation.x = 0;
        if (B.leftLowerLeg)  B.leftLowerLeg.rotation.x = 0;
        if (B.leftFoot)      B.leftFoot.rotation.x = 0;
        if (B.rightUpperLeg) B.rightUpperLeg.rotation.x = 0;
        if (B.rightLowerLeg) B.rightLowerLeg.rotation.x = 0;
        if (B.rightFoot)     B.rightFoot.rotation.x = 0;
        if (B.hips)          B.hips.rotation.y = 0;
      }
      if (parts) {
        if (parts.armL) { parts.armL.rotation.z = 0.25; parts.armL.rotation.x = 0; }
        if (parts.armR) { parts.armR.rotation.z = -0.25; parts.armR.rotation.x = 0; }
      }
    }
  }; // ==================== 随机移动系统（路径化，作为动作大类 WALK） ====================
  App.updateWalkTimer = function updateWalkTimer(dt) {
    App.idleEnergy += dt * 0.5;
    if (!App.currentAction || App.currentAction.type !== App.ActionType.WALK) {
      App.idleWalkTarget = null;
      App.idleWalkProgress = 0;
      return;
    }
    if (App.idleWalkTarget && App.idleWalkProgress < 1) {
      App.idleWalkProgress += App.idleWalkSpeed * dt;
      if (App.idleWalkProgress >= 1) {
        // 多段路径：推进到下一段
        if (App.walkPath.length > 0 && App.walkSegmentIndex + 1 < App.walkPath.length) {
          App.advanceWalkSegment();
        } else {
          // 路径走完
          App.idleWalkProgress = 1;
          App.idleWalkTarget = null;
          App.walkPath = [];
          App.walkSegmentIndex = 0;
          App.currentAction = null;
          App.nextActionTimer = App.ACTION_GAP_MIN + Math.random() * (App.ACTION_GAP_MAX - App.ACTION_GAP_MIN);
          // 通知 AI 自主控制器行走完成
          if (App._onAIWalkComplete) {
            const cb = App._onAIWalkComplete;
            App._onAIWalkComplete = null;
            cb();
          }
        }
      }
      return;
    }
    // AI 驱动的行走不自动生成新路径
    if (!App._aiDrivenWalk) {
      App.pickWalkPath();
    }
  };
  App.advanceWalkSegment = function advanceWalkSegment() {
    if (!App.modelGroup || App.walkPath.length === 0) return;
    App.walkSegmentIndex++;
    const root = App.modelGroup;
    App.idleWalkStart = {
      x: root.position.x,
      z: root.position.z
    };
    App.idleWalkTarget = App.walkPath[App.walkSegmentIndex];
    const dx = App.idleWalkTarget.x - App.idleWalkStart.x;
    const dz = App.idleWalkTarget.z - App.idleWalkStart.z;
    // 朝向角约定与全局一致（模型前方向 +Z，朝向角 = atan2(dx, dz)，同游戏模式 _applyPlayerMovement）
    App.walkFacingAngle = Math.atan2(dx, dz);
    App.idleWalkProgress = 0;
    const segLen = Math.hypot(dx, dz) || 0.5;
    // 速度取值：AI 驱动行走用 AI_LOBBY_WALK_SPEED（自然步行 1.6），其余用大厅漫步慢速
    const walkSpeed = App._aiDrivenWalk
      ? (App.AI_LOBBY_WALK_SPEED || 1.6)
      : App.WALK_SPEED;
    // 去掉 2.5 进度/秒上限：恒定线速度；
    // max(0.5, segLen) 仅兜底超短段，避免除以极小段长
    App.idleWalkSpeed = walkSpeed / Math.max(0.5, segLen);
    App.alignBodyToWalkDirection();
  };
  App.pickWalkPath = function pickWalkPath() {
    const root = App.modelGroup;
    if (!root) return;
    // 一次 walk 动作只走一段，总时长由动作调度控制
    App.walkSegmentsTotal = 1;
    App.walkSegmentIndex = 0;
    App.walkPath = [];
    const halfRange = Math.PI / 6;
    const forwardAngle = root.rotation.y;
    const angle = forwardAngle - halfRange + Math.random() * (halfRange * 2);
    const dist = App.WALK_RANGE * (0.35 + Math.random() * 0.65);
    const tx = root.position.x + Math.cos(angle) * dist;
    const tz = root.position.z + Math.sin(angle) * dist;
    App.walkPath.push({
      x: tx,
      z: tz
    });
    App.idleWalkStart = {
      x: root.position.x,
      z: root.position.z
    };
    App.idleWalkTarget = App.walkPath[0];
    const dx = App.idleWalkTarget.x - App.idleWalkStart.x;
    const dz = App.idleWalkTarget.z - App.idleWalkStart.z;
    // 朝向角约定与全局一致：atan2(dx, dz)
    App.walkFacingAngle = Math.atan2(dx, dz);
    App.idleWalkProgress = 0;
    const segLen = Math.hypot(dx, dz) || 0.5;
    App.idleWalkSpeed = App.WALK_SPEED / Math.max(0.5, segLen);
    App.alignBodyToWalkDirection();
  }; // 让角色身体朝向行走方向 —— 只更新目标朝向，不硬切旋转。
  // 实际旋转由帧循环平滑插值收敛（踱步来回折返时避免 180° 瞬间掉头）。
  App.alignBodyToWalkDirection = function alignBodyToWalkDirection() {
    const root = App.modelGroup;
    if (!root) return;
    const camFaceY = App.computeBodyFaceCam(root);
    // 归一化到 [-PI, PI] 走最短路径（供停止后回看相机使用）
    let faceOff = App.walkFacingAngle - camFaceY;
    while (faceOff > Math.PI) faceOff -= Math.PI * 2;
    while (faceOff < -Math.PI) faceOff += Math.PI * 2;
    App.smoothWalkFaceOff = faceOff;
  }; // ==================== 转圈动作（作为动作大类 TURN） ====================
  App.startTurnAction = function startTurnAction() {
    const root = App.modelGroup;
    if (!root) return;
    const currentY = root.rotation.y;
    const direction = Math.random() < 0.5 ? -1 : 1;
    // 增大转圈角度（135°~540°，最多一圈半）+ 缩短时长（0.7~1.6 秒），圈速明显更快
    const angle = (Math.PI * 0.75 + Math.random() * Math.PI * 2.25) * direction;
    App.turnStartAngle = currentY;
    App.turnTargetAngle = currentY + angle;
    App.turnDuration = 0.7 + Math.random() * 0.9; // 0.7~1.6 秒
    App.turnElapsed = 0;
    App.turnProgress = 0;
    App.currentAction = {
      type: App.ActionType.TURN
    };
    App.addActionWobble('turn');
  };
  App.updateTurnAction = function updateTurnAction(dt) {
    if (!App.currentAction || App.currentAction.type !== App.ActionType.TURN) return;
    App.turnElapsed += dt;
    App.turnProgress = Math.min(1, App.turnElapsed / App.turnDuration);
    const t = App.turnProgress * App.turnProgress * (3 - 2 * App.turnProgress);
    const currentAngle = App.turnStartAngle + (App.turnTargetAngle - App.turnStartAngle) * t;
    const root = App.modelGroup;
    if (root) {
      App.smoothRotY = currentAngle;
      root.rotation.y = currentAngle;
    }
    if (App.turnProgress >= 1) {
      App.currentAction = null;
      App.nextActionTimer = App.ACTION_GAP_MIN + Math.random() * (App.ACTION_GAP_MAX - App.ACTION_GAP_MIN);
    }
  }; // ==================== 跳舞动作（DANCE） ====================
  // 舞蹈种类：温柔慢舞 / 旋转裙舞 / 活力舞 / 摇裙舞 / 腰臀腿律动舞 / 踏步舞 / 扭腰舞 / 蹦跳舞 / 优雅舞
  App.DANCE_TEMPO = 1.35;              // 全局舞蹈节奏倍率（>1 更快更带感）
  App.DANCE_KINDS = [{
    name: 'gentle',
    duration: [2.5, 4.0]
  }, {
    name: 'spin',
    duration: [2.5, 4.0]
  }, {
    name: 'energetic',
    duration: [2.5, 4.0]
  }, {
    name: 'sway',
    duration: [3.0, 4.5]
  }, {
    name: 'rhythm',
    duration: [2.5, 4.0]
  }, {
    name: 'march',
    duration: [2.5, 4.0]
  }, {
    name: 'twist',
    duration: [2.5, 4.0]
  }, {
    name: 'bounce',
    duration: [2.5, 3.5]
  }, {
    name: 'graceful',
    duration: [3.0, 4.5]
  }];
  App.startDanceAction = function startDanceAction() {
    App.danceElapsed = 0;
    App.danceKind = Math.floor(Math.random() * App.DANCE_KINDS.length);
    const kind = App.DANCE_KINDS[App.danceKind];
    App.danceDuration = kind.duration[0] + Math.random() * (kind.duration[1] - kind.duration[0]);
    if (kind.name === 'spin') {
      // 旋转裙舞：每秒转 1~1.8 圈，比 TURN 更快
      App.danceSpinDir = Math.random() < 0.5 ? -1 : 1;
      App.danceSpinSpeed = (1.3 + Math.random() * 0.8) * App.danceSpinDir;
      App.danceSpinStartY = App.modelGroup ? App.modelGroup.rotation.y : 0;
    } else {
      App.danceSpinSpeed = 0;
    }
    // 夹带转身计划：60% 概率在舞曲中插入 1~2 次平滑转身（90°~360°），旋转裙舞除外
    App.danceTurn = null;
    App.danceTurnPlan = [];
    if (kind.name !== 'spin' && Math.random() < 0.6) {
      const turnCount = Math.random() < 0.5 ? 1 : 2;
      for (let i = 0; i < turnCount; i++) {
        App.danceTurnPlan.push({
          at: 0.25 + Math.random() * 0.45 + i * 0.15,
          delta: (Math.PI / 2 + Math.random() * Math.PI * 1.5) * (Math.random() < 0.5 ? -1 : 1)
        });
      }
    }
    App.currentAction = {
      type: App.ActionType.DANCE
    };
    App.addActionWobble('dance');
  };
  App.updateDanceAction = function updateDanceAction(dt) {
    if (!App.currentAction || App.currentAction.type !== App.ActionType.DANCE) return;
    App.danceElapsed += dt;
    const progress = App.danceElapsed / App.danceDuration;
    const B = App.vrmBones;
    if (!B) return;
    const t = App.danceElapsed;
    const bt = t * App.DANCE_TEMPO; // 加速的节拍时间（更快更带感）
    const fadeIn = Math.min(1, progress / 0.2); // 渐入
    const fadeOut = progress > 0.85 ? (1 - progress) / 0.15 : 1; // 渐出
    const blend = fadeIn * fadeOut;
    const kind = App.DANCE_KINDS[App.danceKind] || App.DANCE_KINDS[0];

    // 旋转裙舞：整体转圈（同时自动驱动裙摆撑伞风效）
    if (kind.name === 'spin' && App.modelGroup) {
      const spinAngle = t * App.danceSpinSpeed * Math.PI * 2;
      App.modelGroup.rotation.y = App.danceSpinStartY + spinAngle * blend;
      App.smoothRotY = App.modelGroup.rotation.y;
    }

    if (kind.name === 'gentle') {
      // 温柔慢舞：身体左右轻摆 + 手臂交替摆动
      const swayZ = Math.sin(bt * 2.0) * 0.09 * blend;
      const swayY = Math.cos(bt * 1.3) * 0.06 * blend;
      if (B.spine) {
        B.spine.rotation.z = App.lerp(B.spine.rotation.z, swayZ, 0.08);
        B.spine.rotation.y = App.lerp(B.spine.rotation.y, swayY, 0.08);
      }
      const leftArmWave = Math.sin(bt * 2.5) * 0.42 * blend;
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(leftArmWave * 0.6), 0.1);
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, Math.sin(bt * 2.5 + 0.8) * 0.2 * blend, 0.1);
      }
      if (B.leftLowerArm) {
        B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x, Math.sin(bt * 2.5 + 1.2) * 0.25 * blend, 0.1);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(leftArmWave * 0.6), 0.1);
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, Math.sin(bt * 2.5 - 0.8) * 0.2 * blend, 0.1);
      }
      if (B.rightLowerArm) {
        B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x, Math.sin(bt * 2.5 - 1.2) * 0.25 * blend, 0.1);
      }
      if (B.hips) {
        B.hips.rotation.z = App.lerp(B.hips.rotation.z, swayZ * 0.5, 0.06);
      }
    } else if (kind.name === 'spin') {
      // 旋转裙舞：双手轻抬外展，像在转裙摆
      const armOut = 0.42 + Math.sin(bt * 3.0) * 0.14 * blend;
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(armOut * 0.5), 0.08);
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, -0.18 * blend, 0.08);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(armOut * 0.5), 0.08);
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, -0.18 * blend, 0.08);
      }
      if (B.spine) {
        B.spine.rotation.y = App.lerp(B.spine.rotation.y, Math.sin(bt * 4.0) * 0.05 * blend, 0.08);
      }
    } else if (kind.name === 'energetic') {
      // 活力舞：大幅度手臂摆动 + 身体弹动
      const beat = Math.sin(bt * 4.2) * blend;
      const beat2 = Math.sin(bt * 4.2 + Math.PI) * blend;
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, beat * 0.60, 0.12);
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(beat2 * 0.2), 0.12);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, beat2 * 0.60, 0.12);
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(beat * 0.2), 0.12);
      }
      if (B.spine) {
        B.spine.rotation.x = App.lerp(B.spine.rotation.x, beat * 0.06, 0.1);
        B.spine.rotation.z = App.lerp(B.spine.rotation.z, beat2 * 0.05, 0.1);
      }
      if (B.hips) {
        B.hips.rotation.x = App.lerp(B.hips.rotation.x, beat2 * 0.04, 0.1);
      }
    } else if (kind.name === 'sway') {
      // 摇裙舞：髋部摇摆带动裙摆
      const hipZ = Math.sin(bt * 2.6) * 0.13 * blend;
      const hipY = Math.cos(bt * 2.1) * 0.09 * blend;
      if (B.hips) {
        B.hips.rotation.z = App.lerp(B.hips.rotation.z, hipZ, 0.1);
        B.hips.rotation.y = App.lerp(B.hips.rotation.y, hipY, 0.1);
      }
      if (B.spine) {
        B.spine.rotation.z = App.lerp(B.spine.rotation.z, -hipZ * 0.4, 0.08);
        B.spine.rotation.y = App.lerp(B.spine.rotation.y, hipY * 0.5, 0.08);
      }
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(hipZ * 0.3), 0.08);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(hipZ * 0.3), 0.08);
      }
    } else if (kind.name === 'rhythm') {
      // 腰臀腿律动舞：腰部摆动 + 臀部摇摆 + 双腿交替迈步
      const beat = Math.sin(bt * 3.0) * blend;
      const beat2 = Math.sin(bt * 3.0 + Math.PI) * blend;
      const hipSway = Math.sin(bt * 2.4);
      const hipTilt = Math.cos(bt * 2.4);
      if (B.hips) {
        B.hips.rotation.y = App.lerp(B.hips.rotation.y, hipSway * 0.15 * blend, 0.12);
        B.hips.rotation.z = App.lerp(B.hips.rotation.z, hipTilt * 0.08 * blend, 0.12);
        B.hips.rotation.x = App.lerp(B.hips.rotation.x, beat * 0.05, 0.12);
      }
      if (B.spine) {
        // 腰部与臀部反向摆动，形成自然律动
        B.spine.rotation.y = App.lerp(B.spine.rotation.y, -hipSway * 0.06 * blend, 0.1);
        B.spine.rotation.z = App.lerp(B.spine.rotation.z, -hipTilt * 0.05 * blend, 0.1);
        B.spine.rotation.x = App.lerp(B.spine.rotation.x, beat * 0.05, 0.1);
      }
      // 双腿交替迈步 + 膝盖微弯
      if (B.leftUpperLeg) {
        B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x, beat * 0.34, 0.15);
      }
      if (B.rightUpperLeg) {
        B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x, beat2 * 0.34, 0.15);
      }
      if (B.leftLowerLeg) {
        B.leftLowerLeg.rotation.x = App.lerp(B.leftLowerLeg.rotation.x, beat * 0.12, 0.12);
      }
      if (B.rightLowerLeg) {
        B.rightLowerLeg.rotation.x = App.lerp(B.rightLowerLeg.rotation.x, beat2 * 0.12, 0.12);
      }
      // 手臂随节奏轻摆
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(beat * 0.15), 0.1);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(beat2 * 0.15), 0.1);
      }
    } else if (kind.name === 'march') {
      // 踏步舞：高抬腿交替踏步 + 对侧摆臂 + 身体微弹
      const step = Math.sin(bt * 3.4);
      const step2 = Math.sin(bt * 3.4 + Math.PI);
      const liftL = Math.max(0, step) * blend;
      const liftR = Math.max(0, step2) * blend;
      if (B.leftUpperLeg) {
        B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x, liftL * 0.62, 0.18);
      }
      if (B.rightUpperLeg) {
        B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x, liftR * 0.62, 0.18);
      }
      if (B.leftLowerLeg) {
        B.leftLowerLeg.rotation.x = App.lerp(B.leftLowerLeg.rotation.x, liftL * 0.30, 0.15);
      }
      if (B.rightLowerLeg) {
        B.rightLowerLeg.rotation.x = App.lerp(B.rightLowerLeg.rotation.x, liftR * 0.30, 0.15);
      }
      if (B.spine) {
        B.spine.rotation.x = App.lerp(B.spine.rotation.x, Math.abs(step) * 0.04 * blend, 0.1);
      }
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, step2 * 0.42 * blend, 0.15);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, step * 0.42 * blend, 0.15);
      }
    } else if (kind.name === 'twist') {
      // 扭腰舞：腰部左右扭转 + 髋部反向 + 屈臂摆动
      const twist = Math.sin(bt * 2.8) * blend;
      if (B.spine) {
        B.spine.rotation.y = App.lerp(B.spine.rotation.y, twist * 0.34, 0.14);
      }
      if (B.hips) {
        B.hips.rotation.y = App.lerp(B.hips.rotation.y, -twist * 0.18, 0.12);
      }
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(0.25), 0.1);
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, twist * 0.15, 0.1);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(0.25), 0.1);
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, -twist * 0.15, 0.1);
      }
      if (B.leftLowerArm) {
        B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x, -0.5 * blend, 0.1);
      }
      if (B.rightLowerArm) {
        B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x, -0.5 * blend, 0.1);
      }
    } else if (kind.name === 'bounce') {
      // 蹦跳舞：屈膝弹跳 + 双臂上下挥动 + 身体微弹
      const b = Math.abs(Math.sin(bt * 3.6)) * blend;
      const b2 = Math.abs(Math.cos(bt * 3.6)) * blend;
      if (B.leftUpperLeg) {
        B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x, b * 0.22, 0.15);
      }
      if (B.rightUpperLeg) {
        B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x, b * 0.22, 0.15);
      }
      if (B.leftLowerLeg) {
        B.leftLowerLeg.rotation.x = App.lerp(B.leftLowerLeg.rotation.x, b * 0.18, 0.12);
      }
      if (B.rightLowerLeg) {
        B.rightLowerLeg.rotation.x = App.lerp(B.rightLowerLeg.rotation.x, b * 0.18, 0.12);
      }
      if (B.spine) {
        B.spine.rotation.x = App.lerp(B.spine.rotation.x, b * 0.05, 0.1);
      }
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, b2 * 0.55, 0.15);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, b2 * 0.55, 0.15);
      }
      if (B.leftLowerArm) {
        B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x, b2 * 0.30, 0.12);
      }
      if (B.rightLowerArm) {
        B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x, b2 * 0.30, 0.12);
      }
    } else if (kind.name === 'graceful') {
      // 优雅舞：缓慢手臂画圆 + 轻微屈膝 + 头微倾
      const s = Math.sin(bt * 1.6) * blend;
      const c = Math.cos(bt * 1.6) * blend;
      if (B.leftUpperArm) {
        B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, c * 0.42, 0.08);
        B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, App.armZ(s * 0.25), 0.08);
      }
      if (B.rightUpperArm) {
        B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, -c * 0.42, 0.08);
        B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, -App.armZ(s * 0.25), 0.08);
      }
      if (B.leftLowerArm) {
        B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x, c * 0.25, 0.08);
      }
      if (B.rightLowerArm) {
        B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x, -c * 0.25, 0.08);
      }
      if (B.leftUpperLeg) {
        B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x, s * 0.12, 0.08);
      }
      if (B.rightUpperLeg) {
        B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x, -s * 0.12, 0.08);
      }
      if (B.head) {
        B.head.rotation.x = App.lerp(B.head.rotation.x, s * 0.08, 0.06);
      }
    }
    // 夹带转身：舞曲中段平滑转身（旋转裙舞本身在转圈，跳过）
    if (kind.name !== 'spin' && App.modelGroup) {
      if (!App.danceTurn && App.danceTurnPlan && App.danceTurnPlan.length > 0 && progress >= App.danceTurnPlan[0].at) {
        const plan = App.danceTurnPlan.shift();
        App.danceTurn = {
          startY: App.modelGroup.rotation.y,
          targetY: App.modelGroup.rotation.y + plan.delta,
          t: 0,
          dur: 0.6 + Math.random() * 0.5
        };
      }
      if (App.danceTurn) {
        App.danceTurn.t += dt;
        const k = App.danceTurn.t / App.danceTurn.dur;
        const ease = k * k * (3 - 2 * k);
        const cur = App.danceTurn.startY + (App.danceTurn.targetY - App.danceTurn.startY) * Math.min(1, ease);
        App.modelGroup.rotation.y = cur;
        App.smoothRotY = cur;
        if (App.danceTurn.t >= App.danceTurn.dur) App.danceTurn = null;
      }
    }
    if (progress >= 1) {
      App.currentAction = null;
      App.danceTurn = null;
      App.danceTurnPlan = [];
      App.nextActionTimer = App.ACTION_GAP_MIN + Math.random() * (App.ACTION_GAP_MAX - App.ACTION_GAP_MIN);
    }
  }; // ==================== 统一动作调度器 ====================
  App.startPoseAction = function startPoseAction() {
    App.posePhase = 'idle';
    App.poseTimer = 0;
    App.currentAction = {
      type: App.ActionType.POSE
    };
    App.addActionWobble('pose');
  };
  App.startWalkAction = function startWalkAction() {
    App.currentAction = {
      type: App.ActionType.WALK
    };
    App.addActionWobble('walk');
  };
  App.pickNextActionType = function pickNextActionType() {
    const types = [App.ActionType.POSE, App.ActionType.WALK, App.ActionType.TURN, App.ActionType.DANCE];
    return types[Math.floor(Math.random() * types.length)];
  };
  App.updateActionScheduler = function updateActionScheduler(dt) {
    // Mixamo 动作播放中：
    // - 循环动作（待机/姿势/舞姿）：播完完整一遍（按 clip 实际时长）后才允许轮换，
    //   不再中途掐断——之前 2.8s 就摇骰子强制释放，导致 5~37s 的长动作
    //   （舞蹈/姿势）永远只播个开头就被切走，看起来“播不完整”。
    //   只有超长循环动作（>30s，如 phone_call 37s）才设上限，避免长期霸占。
    // - 单次动作（情绪表达）：播完正常自动释放（finished → 直接硬切下一个随机动作），
    //   取消 5s 硬上限——之前长单次动作（laugh 8s / victory_cheer 8s / cry 6s）
    //   播到一半就被 stopMixamoClip 掐断。仅超长单次动作（>30s）兜底释放。
    if (App._mixamoActiveClip) {
      const start = App._mixamoActiveClipStart || 0;
      const playedMs = performance.now() - start;
      if (App._mixamoActiveClipLoop) {
        const clipDurMs = (App._mixamoActiveAction && App._mixamoActiveAction.getClip().duration || 0) * 1000;
        // 完整播完一遍（含 0.5s 余量）→ 立即直接切换下一个随机动作（硬切），
        // 不留静息空窗、不等间隔——彻底消除动作间卡顿；超长循环动作（>30s）30s 兜底
        const fullMs = Math.min(clipDurMs > 0 ? clipDurMs + 500 : 2800, 30000);
        if (playedMs < fullMs) return;
        if (!App.tryStartLibraryAction || !App.tryStartLibraryAction()) {
          // 库无可播动作 → 回退程序式微动作（不摇骰子，立即接上）
          App.nextActionTimer = 0;
          App.stopMixamoClip();
        }
      } else if (playedMs >= 30000 && !App._mixamoTailActive) {
        // 单次动作 30s 硬上限（兜底：finished 丢失/超长 clip 都能释放）
        App.nextActionTimer = 0.3;
        App.stopMixamoClip();
      }
      return;
    }
    if (App.currentAction) return; // 各动作自己负责结束
    // 注：VR 模式下不再禁用随机动作——非 VR 可用的 POSE/WALK/TURN/DANCE 在 VR 中同样解锁；
    // TURN 与面向用户逻辑的冲突已由 updateXRFaceUser 对 TURN 的跳过处理消解。
    App.nextActionTimer -= dt;
    if (App.nextActionTimer <= 0) {
      // 统一调度：动作库已就绪（动态模式：配置加载完即就绪，动作按需加载+预取）
      // → 从已加载动作里随机播放；库未就绪或没有已加载动作时回退到手写程序式动作
      // （POSE/WALK/TURN/DANCE，启动期由它撑场）
      if (App.tryStartLibraryAction && App._animLibraryLoaded) {
        const clipName = App.tryStartLibraryAction();
        if (clipName) return; // 已由动作库接管
      }
      let type = App.pickNextActionType();
      // VR 大厅角色位置固定：WALK 会走离固定站位，换成原地动作（POSE/TURN/DANCE）
      if (App.xrPresenting && type === App.ActionType.WALK) {
        const stay = [App.ActionType.POSE, App.ActionType.TURN, App.ActionType.DANCE];
        type = stay[Math.floor(Math.random() * stay.length)];
      }
      if (type === App.ActionType.POSE) App.startPoseAction();else if (type === App.ActionType.WALK) App.startWalkAction();else if (type === App.ActionType.TURN) App.startTurnAction();else if (type === App.ActionType.DANCE) App.startDanceAction();
    }
  };
  // 动作触发的轻微惯性晃动
  App.addActionWobble = function addActionWobble(actionType) {
    if (!App.modelGroup) return;
    const cw = App.clickWobble;
    const str = { pose: 0.003, walk: 0.004, turn: 0.005, dance: 0.006 };
    const s = str[actionType] || 0.0;
    if (s === 0) return;
    // 随机方向的小速度冲量，叠加到已有晃动上
    const angle = Math.random() * Math.PI * 2;
    cw.velX += Math.cos(angle) * s;
    cw.velZ += Math.sin(angle) * s;
    cw.velY += (Math.random() - 0.5) * s * 0.5;
    cw.rotVelZ += (Math.random() - 0.5) * s * 0.4;
    cw.rotVelX += (Math.random() - 0.5) * s * 0.4;
    cw.active = true;
  }; // 计算当前帧的姿态混合因子（带缓动曲线）
  App.computePoseBlend = function computePoseBlend() {
    let target = App.poseBlendTarget;
    const diff = target - App.poseBlend;
    if (Math.abs(diff) < 0.001) {
      App.poseBlend = target;
      return App.poseBlend;
    }
    // 自适应速度 + smoothstep 缓动：让动作起始和收尾都更柔和
    const speed = App.posePhase === 'entering' ? 0.045 : 0.055;
    let step = diff * speed;
    const t01 = Math.max(0, Math.min(1, App.poseBlend));
    const ease = t01 * t01 * (3 - 2 * t01); // smoothstep
    step *= 0.6 + 0.4 * (1 - Math.abs(ease - 0.5) * 2);
    App.poseBlend += step;
    if (Math.abs(target - App.poseBlend) < 0.003) App.poseBlend = target;
    return App.poseBlend;
  }; // 第一人称探索模式
  App.fpvMode = false;
  App.fpvJustExited = false; // 刚退出 FPV，下一帧立即 snap 角色朝向相机
  App.fpvPos = new THREE.Vector3(0, 1.6, 3);
  App.fpvYaw = 0;
  App.fpvPitch = 0;
  App.fpvSavedAutoRotate = true;
  App.FPV_HEIGHT = 1.6;
  App.FPV_MOVE_SPEED = 3.0; // 单位/秒
  App.FPV_LOOK_SENSITIVITY = 0.0025;
  App.FPV_PITCH_LIMIT = Math.PI / 2 - 0.05;
  App.fpvKeys = {};
  App.fpvMoveVec = {
    x: 0,
    y: 0
  }; // 摇杆输入 (-1..1)
  App.fpvMovePointerId = null; // 移动拖拽的 pointerId
  App.fpvLookPointerId = null; // 转向拖拽的 pointerId
  App.fpvMoveOrigin = {
    x: 0,
    y: 0
  }; // 浮动摇杆诞生点（屏幕坐标）
  App.fpvLookLastX = 0; // VRM humanoid 骨骼缓存
  App.fpvLookLastY = 0;
  App.vrmBones = {};
  App.gltfLoader = new GLTFLoader();
  // 注册 Draco 解码器：支持 KHR_draco_mesh_compression 压缩过的模型
  const dracoLoader = new DRACOLoader();
  // 必须是绝对静态路径：/static 挂载 web/ 根 → /static/vendor/three/... 命中解码器
  // 不要写成相对的，页面在根 URL '/' 时 resolve 成顶级 /vendor → 404，压缩模型直接加载失败
  dracoLoader.setDecoderPath('/static/vendor/three/examples/jsm/libs/draco/gltf/');
  App.gltfLoader.setDRACOLoader(dracoLoader);
  App.gltfLoader.register(parser => new VRMLoaderPlugin(parser));
  App.initThree = function initThree() {
    App.scene = new THREE.Scene();
    App.scene.fog = new THREE.Fog(0x0a0a14, 8, 20);
    const w = App.canvas.clientWidth,
      h = App.canvas.clientHeight;
    App.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100);
    App.camera.position.set(0, App.cameraHeight, App.cameraDistance);
    App.camera.lookAt(0, 1.0, 0);
    App.renderer = new THREE.WebGLRenderer({
      canvas: App.canvas,
      antialias: App._useAA,
      alpha: true,
      powerPreference: App.perfTier === 'high' ? 'high-performance' : 'low-power'
    });
    App.renderer.setSize(w, h, false);
    App.renderer.setPixelRatio(App._targetDPR);
    App.renderer.outputColorSpace = THREE.SRGBColorSpace;
    App.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    App.renderer.toneMappingExposure = 1.0;
    // WebXR 支持：启用渲染器的 XR 能力（无 WebXR 设备时无副作用）
    App.renderer.xr.enabled = true;

    // 注册 KTX2 纹理解码器：支持 KHR_texture_basisu 压缩纹理（烘焙模型常用），
    // 必须在 renderer 创建后 detectSupport（依赖 WebGL 压缩纹理能力探测）
    const ktx2Loader = new KTX2Loader();
    ktx2Loader.setTranscoderPath('/static/vendor/three/examples/jsm/libs/basis/');
    ktx2Loader.detectSupport(App.renderer);
    App.gltfLoader.setKTX2Loader(ktx2Loader);

    // 灯光 (增强三盏主光 + 顶部聚光)
    App.scene.add(new THREE.AmbientLight(0x4060a0, 0.5));
    const key = new THREE.DirectionalLight(0xffffff, 1.5);
    key.position.set(2, 4, 3);
    App.scene.add(key);
    const rim = new THREE.DirectionalLight(0xff6b9d, 1.2);
    rim.position.set(-2, 3, -2);
    App.scene.add(rim);
    const fill = new THREE.PointLight(0x7c5cff, 1.5, 8);
    fill.position.set(0, 1.5, 2.5);
    App.scene.add(fill);
    // 保存到 parts：VR 世界统一平移时点光源需跟随（方向光/环境光无位置，无需平移）
    App.parts.fillLight = fill;
    // 顶部柔光 (模拟天光)
    const top = new THREE.DirectionalLight(0x88aaff, 0.5);
    top.position.set(0, 6, 0);
    App.scene.add(top);

    // 接触阴影 (脚下暗圈，增强接地感)
    const shadowGeo = new THREE.CircleGeometry(0.6, 32);
    const shadowMat = new THREE.MeshBasicMaterial({
      color: 0x000000,
      transparent: true,
      opacity: 0.45,
      depthWrite: false
    });
    const contactShadow = new THREE.Mesh(shadowGeo, shadowMat);
    contactShadow.rotation.x = -Math.PI / 2;
    contactShadow.position.y = 0.005;
    contactShadow.scale.set(1.2, 0.5, 1); // 椭圆形阴影
    App.scene.add(contactShadow);
    App.parts.contactShadow = contactShadow;

    // 地面光晕 (加大加亮)
    const glow = new THREE.Mesh(new THREE.CircleGeometry(2.2, 48), new THREE.MeshBasicMaterial({
      color: 0x7c5cff,
      transparent: true,
      opacity: 0.35,
      side: THREE.DoubleSide,
      depthWrite: false
    }));
    glow.rotation.x = -Math.PI / 2;
    glow.position.y = 0.01;
    App.scene.add(glow);
    App.parts.glow = glow;
    App.addStars();
    App.smoothRotY = 0;
    App.smoothRotX = 0;
    App.clock = new THREE.Clock();
    window.addEventListener('resize', App.onResize);

    // 拖动旋转 / 移动模式下的选中+平移 / FPV 左半屏移动+右半屏转向
    let dragging = false,
      lastX = 0,
      lastY = 0,
      wasPinching = false; // 标记刚刚发生过双指捏合，防止 pointerup 误触发 click
    App.canvas.addEventListener('pointerdown', e => {
      App.recordInteraction();
      // 游戏模式下禁用拖拽旋转（用户和角色是一体）
      // VR 模式下禁用鼠标/触摸交互（手柄/陀螺仪接管）
      if (App.xrMode && App.xrMode !== 'off') return;
      if (App.fpvMode) {
        App.canvas.setPointerCapture(e.pointerId);
        const rect = App.canvas.getBoundingClientRect();
        const localX = e.clientX - rect.left;
        if (localX < rect.width / 2 && App.fpvMovePointerId === null) {
          // 左半屏 → 浮动摇杆移动
          App.fpvMovePointerId = e.pointerId;
          App.fpvMoveOrigin.x = e.clientX;
          App.fpvMoveOrigin.y = e.clientY;
          App.showFloatingJoystick(e.clientX, e.clientY);
        } else if (App.fpvLookPointerId === null) {
          // 右半屏 → 转向（也支持点击角色）
          App.fpvLookPointerId = e.pointerId;
          App.fpvLookLastX = e.clientX;
          App.fpvLookLastY = e.clientY;
          App.clickStartPos.x = e.clientX;
          App.clickStartPos.y = e.clientY;
          App.canvas.style.cursor = 'grabbing';
        }
        return;
      }
      // 多指捏合进行中时，后续 pointerdown 不覆盖状态（否则会重置 dragging/clickStartPos）
      if (App.pinching || wasPinching) return;
      dragging = true;
      App.isDragging = true;
      App.dragTotalRot = 0;
      App.clickStartPos.x = e.clientX;
      App.clickStartPos.y = e.clientY;
      lastX = e.clientX;
      lastY = e.clientY;
      App.canvas.setPointerCapture(e.pointerId);
      if (App.moveMode) App.onMovePointerDown(e);
    });
    App.canvas.addEventListener('pointerup', e => {
      // 游戏模式下跳过所有交互
      // VR 模式下禁用鼠标/触摸交互
      if (App.xrMode && App.xrMode !== 'off') return;
      if (App.fpvMode) {
        if (e.pointerId === App.fpvMovePointerId) {
          App.hideFloatingJoystick();
        }
        if (e.pointerId === App.fpvLookPointerId) {
          App.fpvLookPointerId = null;
          App.canvas.style.cursor = 'grab';
          // 右半屏短点击 = 点击角色（非拖拽转向）
          const dist = Math.hypot(e.clientX - App.clickStartPos.x, e.clientY - App.clickStartPos.y);
          if (dist < 5 && App.currentAvatar) {
            App.handleCharacterClick(e);
          }
        }
        return;
      }
      // 双指捏合刚结束时，跳过 pointerup 的点击检测（曾被 setPointerCapture 覆盖 clickStartPos）
      const skipClick = wasPinching;
      wasPinching = false;
      dragging = false;
      App.isDragging = false;
      // 点击角色检测（非移动模式，非FPV，短距离点击=非拖拽，且非捏合结束）
      if (!App.moveMode && !App.fpvMode && !skipClick) {
        const dist = Math.hypot(e.clientX - App.clickStartPos.x, e.clientY - App.clickStartPos.y);
        if (dist < 5) {
          App.handleCharacterClick(e);
          App.dragTotalRot = 0;
        } else if (App.dragTotalRot > App.DRAG_THRESHOLD) {
          // 相机环绕强度分级反应
          const total = App.dragTotalRot;
          let msg;
          if (total > 10) {
            msg = `（用户正绕着你转了${Math.round(total / Math.PI)}圈，仔细打量你的全身上下，似乎对你充满了好奇）`;
          } else if (total > 3) {
            msg = '（用户绕着你转了好几圈，从各个角度仔细端详你）';
          } else {
            msg = '（用户微微移动了视角，换了个角度看你）';
          }
          App.sendAIAction(msg);
          App.dragTotalRot = 0;
        }
        App.dragTotalRot = 0;
      }
      if (App.moveMode && App.selectedTarget) App.saveSceneState();
    });
    App.canvas.addEventListener('pointermove', e => {
      // VR 模式下禁用鼠标/触摸交互
      if (App.xrMode && App.xrMode !== 'off') return;
      if (App.fpvMode) {
        if (e.pointerId === App.fpvMovePointerId) {
          // 浮动摇杆：从起点计算偏移 → 移动方向
          App.updateFloatingJoystick(e.clientX - App.fpvMoveOrigin.x, e.clientY - App.fpvMoveOrigin.y);
        } else if (e.pointerId === App.fpvLookPointerId) {
          App.fpvYaw -= (e.clientX - App.fpvLookLastX) * App.FPV_LOOK_SENSITIVITY * 2;
          App.fpvPitch -= (e.clientY - App.fpvLookLastY) * App.FPV_LOOK_SENSITIVITY * 2;
          App.fpvPitch = Math.max(-App.FPV_PITCH_LIMIT, Math.min(App.FPV_PITCH_LIMIT, App.fpvPitch));
          App.fpvLookLastX = e.clientX;
          App.fpvLookLastY = e.clientY;
        }
        return;
      }
      if (!dragging || App.pinching) return;
      // 拖拽时退出聚焦（恢复自由视角）
      App.exitFocusMode();
      if (App.moveMode) {
        App.onMovePointerMove(e);
      } else {
        const dY = (e.clientX - lastX) * 0.015;
        const dX = (e.clientY - lastY) * 0.01;
        App.dragOrbitYaw -= dY;
        App.dragOrbitPitch += dX;
        // 隐私护栏：夹累加值本身，否则拖到极限后反向拖要先「还债」，手感像橡皮筋
        if (!App.IS_ADMIN) {
          App.dragOrbitPitch = Math.max(-App.USER_ORBIT_PITCH_LIMIT, Math.min(App.USER_ORBIT_PITCH_LIMIT, App.dragOrbitPitch));
        }
        App.dragTotalRot += Math.abs(dY) + Math.abs(dX);
        // 拖拽控制相机环绕角色：水平拖拽=水平环绕，垂直拖拽=上下打量
        lastX = e.clientX;
        lastY = e.clientY;
      }
    });

    // 滚轮缩放 (桌面) / FPV 模式下调整高度
    App.canvas.addEventListener('wheel', e => {
      App.recordInteraction();
      e.preventDefault();
      // 游戏模式下禁用滚轮缩放（相机固定后面）和移动模式缩放
      // VR 模式下禁用滚轮
      if (App.xrMode && App.xrMode !== 'off') return;
      if (App.fpvMode) {
        // 第一人称模式下：滚轮调整视点高度
        const minEyeY = App.IS_ADMIN ? 0.3 : App.USER_FPV_MIN_HEIGHT;
        App.fpvPos.y = THREE.MathUtils.clamp(App.fpvPos.y - e.deltaY * 0.005, minEyeY, 8);
        return;
      }
      if (App.moveMode && App.selectedTarget) {
        App.scaleSelectedTarget(e.deltaY > 0 ? 0.95 : 1.05);
        App.debouncedSaveScene();
      } else {
        App.camZoom += e.deltaY * 0.006;
        App.camZoom = Math.max(App.userMinZoom(), Math.min(App.MAX_ZOOM, App.camZoom));
        App.exitFocusMode();
        App.debouncedSaveScene();
        // 缩放防抖：停止滚动300ms后触发AI反应
        const delta = e.deltaY * 0.006;
        App.zoomNet += delta;
        App.zoomAbsTotal += Math.abs(delta);
        clearTimeout(App.zoomDebounceTimer);
        App.zoomDebounceTimer = setTimeout(() => {
          if (App.zoomAbsTotal > App.ZOOM_THRESHOLD) {
            const zoomIn = App.zoomNet < 0;
            const intensity = App.zoomAbsTotal > 1.5 ? '猛地' : '';
            const msg = zoomIn ? `（用户${intensity}把脸凑到了你面前，近到你能感受到Ta的呼吸，你的心跳开始加速）` : `（用户${intensity}退后几步拉开了距离，远远地看着你）`;
            App.sendAIAction(msg);
          }
          App.zoomNet = 0;
          App.zoomAbsTotal = 0;
        }, 300);
      }
    }, {
      passive: false
    });

    // 触摸：非 FPV 模式下处理捏合缩放（FPV 由 pointer 事件统一处理）
    let pinchDist = 0;
    App.canvas.addEventListener('touchstart', e => {
      App.recordInteraction();
      if (App.fpvMode) return;
      if (App.xrMode && App.xrMode !== 'off') return;
      if (e.touches.length === 2) {
        App.pinching = true;
        wasPinching = true;
        dragging = false;
        pinchDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      }
    }, {
      passive: true
    });
    App.canvas.addEventListener('touchmove', e => {
      App.recordInteraction();
      if (App.fpvMode) return;
      if (App.xrMode && App.xrMode !== 'off') return;
      if (e.touches.length === 2 && App.pinching) {
        e.preventDefault();
        App.exitFocusMode();
        const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
        if (pinchDist > 0) {
          if (App.moveMode && App.selectedTarget) {
            App.scaleSelectedTarget(d / pinchDist);
            App.debouncedSaveScene();
          } else {
            const prevZoom = App.camZoom;
            const ratio = pinchDist / d;
            App.camZoom *= Math.pow(ratio, App.PINCH_SENSITIVITY);
            App.camZoom = Math.max(App.userMinZoom(), Math.min(App.MAX_ZOOM, App.camZoom));
            const delta = App.camZoom - prevZoom;
            App.zoomNet += delta;
            App.zoomAbsTotal += Math.abs(delta);
            clearTimeout(App.zoomDebounceTimer);
            App.zoomDebounceTimer = setTimeout(() => {
              if (App.zoomAbsTotal > App.ZOOM_THRESHOLD) {
                const zoomIn = App.zoomNet < 0;
                const intensity = App.zoomAbsTotal > 1.5 ? '猛地' : '';
                const msg = zoomIn ? `（用户${intensity}把镜头推近，凑到了你面前）` : `（用户${intensity}把镜头拉远了）`;
                App.sendAIAction(msg);
              }
              App.zoomNet = 0;
              App.zoomAbsTotal = 0;
            }, 300);
            App.debouncedSaveScene();
          }
        }
        pinchDist = d;
      }
    }, {
      passive: false
    });
    App.canvas.addEventListener('touchend', e => {
      if (App.fpvMode) return;
      if (App.xrMode && App.xrMode !== 'off') return;
      if (e.touches.length < 2) {
        App.pinching = false;
        pinchDist = 0;
      }
    }, {
      passive: true
    });

    // FPV：键盘移动（摇杆由画布 pointer 事件统一处理，无需单独监听）
    document.addEventListener('keydown', App.onFPVKeyDown);
    document.addEventListener('keyup', App.onFPVKeyUp);
    // 统一由 renderer 驱动动画循环：非 XR 走 rAF，进入 WebXR 会话时 three.js
    // 在 sessionstart/sessionend 自动切换 XR 帧循环，避免 VR 入口手动切换
    // requestAnimationFrame / setAnimationLoop 造成双重循环或会话失败后画面停摆
    App.renderer.setAnimationLoop(App.animate);
    // 帧循环刚起：此刻可能已经处于聊天全屏静默（手机上模型加载比用户点 ⤢ 慢），
    // 立刻按回去 —— 否则全屏里模型一直在渲染，手机发烫且再也回不到静默
    if (App.syncQuietLoop) App.syncQuietLoop();
  };
  App.addStars = function addStars() {
    // 按当前性能档位的 _starCount 生成粒子：default=80、low=不生成（省显存/GPU）
    const count = App._starCount > 0 ? App._starCount : 0;
    if (count <= 0) { App.starField = null; return; }
    const geo = new THREE.BufferGeometry();
    const pos = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 20;
      pos[i * 3 + 1] = Math.random() * 6 + 1;
      pos[i * 3 + 2] = (Math.random() - 0.5) * 14 - 4;
    }
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    App.starField = new THREE.Points(geo, new THREE.PointsMaterial({
      color: 0xffffff,
      size: 0.04,
      transparent: true,
      opacity: 0.7,
      sizeAttenuation: true
    }));
    App.scene.add(App.starField);
  };
  /* ---------- 开始 3D 背景场景加载 ---------- */
  App.onResize = function onResize() {
    if (!App.camera || !App.renderer) return;
    const w = App.canvas.clientWidth,
      h = App.canvas.clientHeight;
    App.camera.aspect = w / h;
    App.camera.updateProjectionMatrix();
    App.renderer.setSize(w, h, false);
    // 分辨率跟随物理像素密度变化（旋转/跨屏拖动后 devicePixelRatio 可能改变），
    // 保证画布始终以原生物理分辨率渲染，不产生上采样模糊
    if (App._nativeDPR) {
      const native = App._nativeDPR();
      if (native !== App._targetDPR) {
        App._targetDPR = native;
        App.renderer.setPixelRatio(native);
      }
    }
  };
  /* ============================================================
   *  3D 模型加载 (GLTF / VRM)
   * ============================================================ */
});
